import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from h3_48gb.cli import (
    CliError,
    RunSpec,
    build_parser,
    main,
    run_doctor,
    run_generate,
    run_list,
    run_resume,
    run_status,
    spec_from_args,
)
from h3_48gb.cli import _checkpoint_path_for, DEFAULT_OUTDIR


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
        outdir=DEFAULT_OUTDIR, tag="demo",
    )


def test_prompt_file_matches_the_shell(tmp_path):
    """`$(cat file)` strips every trailing newline, and every measured run to date was launched
    that way. Leaving one on would change the prompt by a character, change identity_digest, and
    orphan every checkpoint on disk.

    The text comparison below is diagnostically useful but is not what is actually at stake:
    identity is built from arguments bound to the pipeline's own `__call__` signature (a stray
    argument there raises `TypeError`, so that source is guarded) plus the free-form
    `checkpoint_identity_extra()` dict, which is an ordinary dict a text-only comparison would not
    notice a leak through. Comparing `_checkpoint_path_for`'s output directly is what actually
    confirms a run started with `--prompt-file` resumes the same checkpoint as one started
    positionally -- the same direct-comparison pattern
    `test_resume_checkpoint_path_changes_with_the_request` already uses.
    """
    path = tmp_path / "p.txt"
    path.write_text("a cat\n\n\n")

    from_file = spec_from_args(build_parser().parse_args(
        ["generate", "--prompt-file", str(path), "--outdir", str(tmp_path)]))
    positional = spec_from_args(build_parser().parse_args(
        ["generate", "a cat", "--outdir", str(tmp_path)]))

    assert from_file.prompt == positional.prompt

    pipe = _StubPipe()
    assert (_checkpoint_path_for(from_file, pipe, tmp_path / "checkpoints")
            == _checkpoint_path_for(positional, pipe, tmp_path / "checkpoints"))


def test_prompt_and_prompt_file_together_are_refused(tmp_path):
    path = tmp_path / "p.txt"
    path.write_text("a cat\n")
    with pytest.raises(CliError) as excinfo:
        spec_from_args(build_parser().parse_args(
            ["generate", "a dog", "--prompt-file", str(path), "--outdir", str(tmp_path)]))
    assert excinfo.value.code == "prompt_both_given"


def test_no_prompt_at_all_is_refused(tmp_path):
    with pytest.raises(CliError) as excinfo:
        spec_from_args(build_parser().parse_args(["generate", "--outdir", str(tmp_path)]))
    assert excinfo.value.code == "prompt_missing"


def test_prompt_file_pointing_nowhere_is_refused(tmp_path):
    missing = tmp_path / "missing.txt"
    with pytest.raises(CliError) as excinfo:
        spec_from_args(build_parser().parse_args(
            ["generate", "--prompt-file", str(missing), "--outdir", str(tmp_path)]))
    assert excinfo.value.code == "prompt_file_not_found"


def test_prompt_file_that_is_not_valid_utf8_is_refused(tmp_path):
    path = tmp_path / "bad.txt"
    path.write_bytes(b"\xff\xfe\x00\x01")
    with pytest.raises(CliError) as excinfo:
        spec_from_args(build_parser().parse_args(
            ["generate", "--prompt-file", str(path), "--outdir", str(tmp_path)]))
    assert excinfo.value.code == "prompt_file_unreadable"


def test_prompt_file_of_only_newlines_is_refused_as_empty(tmp_path):
    """The shell equivalent, `P="$(cat file)"`, would silently substitute an empty string, and a
    run would spend hours generating a clip of nothing -- exactly the zone
    `test_prompt_file_matches_the_shell` exists to police, seen from the other side.
    """
    path = tmp_path / "empty.txt"
    path.write_text("\n\n\n")
    with pytest.raises(CliError) as excinfo:
        spec_from_args(build_parser().parse_args(
            ["generate", "--prompt-file", str(path), "--outdir", str(tmp_path)]))
    assert excinfo.value.code == "prompt_file_empty"


def test_rejects_geometry_the_port_cannot_pack():
    parser = build_parser()
    args = parser.parse_args(["generate", "a cat", "--height", "432"])
    try:
        spec_from_args(args)
    except CliError as exc:
        assert "multiple of 32" in str(exc)
        assert exc.code == "geometry_not_multiple_of_32"
        assert exc.detail == {"height": 432}
    else:
        raise AssertionError("432 is not a multiple of 32 and must be rejected up front")


class _StubResult:
    video = np.zeros((5, 32, 32, 3), dtype=np.uint8)
    audio = np.zeros((2, 8000), dtype=np.float32)
    sample_rate = 32000
    seconds_per_step = 1.5


def test_raw_arrays_are_written_before_encoding(tmp_path):
    """A failure in mp4 encoding must not destroy hours of compute."""
    def exploding_save_mp4(*args, **kwargs):
        raise RuntimeError("ffmpeg unavailable")

    spec = RunSpec(prompt="x", width=64, height=64, duration=1.0, steps=31, seed=0,
                   checkpoint=tmp_path, outdir=tmp_path, tag="t")
    try:
        run_generate(spec, pipeline_factory=lambda _: (lambda **kw: _StubResult()),
                     save_mp4_fn=exploding_save_mp4)
    except RuntimeError:
        pass
    assert (tmp_path / "h3-t-64x64-raw.npz").exists(), "raw arrays must survive an encoder failure"


def test_truncated_raw_file_is_not_left_at_destination(tmp_path):
    """Crash after temp write but before rename must not corrupt destination.

    Exercises the atomic write pattern's critical window: after savez_compressed
    writes the temp file but before os.replace commits it. Verifies that temp
    file is cleaned up and destination remains absent.
    """
    import unittest.mock as mock
    import glob

    spec = RunSpec(prompt="x", width=64, height=64, duration=1.0, steps=31, seed=0,
                   checkpoint=tmp_path, outdir=tmp_path, tag="t")

    # Patch os.replace to fail *after* the real savez_compressed has written the temp file.
    # This exercises the window between "write temp" and "atomic rename".
    original_replace = os.replace

    def failing_replace(src, dst):
        # os.replace is called once per atomic write, so this fails on the npz rename.
        raise RuntimeError("disk full during rename")

    try:
        with mock.patch("os.replace", side_effect=failing_replace):
            run_generate(spec, pipeline_factory=lambda _: (lambda **kw: _StubResult()))
    except RuntimeError as e:
        assert "disk full" in str(e)

    raw_path = tmp_path / "h3-t-64x64-raw.npz"
    # Destination must not exist (rename failed, so it was never created)
    assert not raw_path.exists(), "failed rename must not leave a destination file"

    # Temp file must be cleaned up by the except handler
    temp_files = glob.glob(str(tmp_path / ".h3-t-64x64-raw.tmp-*"))
    assert not temp_files, "temp file must be cleaned up on failure"


def test_rejects_mismatched_schedule(tmp_path):
    """Multi-hour run must not begin on a schedule that cannot finish.

    The refusal is in `RunSpec.__post_init__`, so it fires the moment the request exists — before
    a pipeline is loaded, before a checkpoint path is computed. See
    `test_resume_blames_the_schedule_not_a_missing_checkpoint` for why that ordering matters.
    """
    try:
        RunSpec(prompt="x", width=64, height=64, duration=1.0, steps=30, seed=0,
                checkpoint=tmp_path, outdir=tmp_path, tag="t")
    except CliError as exc:
        assert "31" in str(exc), "error message must name the baked value"
        assert "AdaLN" in str(exc), "error message must explain why"
        assert exc.code == "schedule_not_baked"
    else:
        raise AssertionError("mismatched schedule must be rejected before compute")


def test_resume_blames_the_schedule_not_a_missing_checkpoint(tmp_path):
    """`resume --steps 20` is a schedule error, and used to be reported as `checkpoint_not_found`.

    `run_resume` computes the checkpoint path (and loads a pipeline to do it) before `run_generate`
    ever sees the spec, so a `--steps` check living in `run_generate` was unreachable: the user was
    told a checkpoint was missing, which was true but not the reason their command failed.
    """
    args = build_parser().parse_args(
        ["resume", "a cat", "--steps", "20", "--outdir", str(tmp_path)])
    try:
        spec_from_args(args)
    except CliError as exc:
        assert exc.code == "schedule_not_baked", (
            f"a bad --steps must be reported as a schedule error, got {exc.code!r}")
    else:
        raise AssertionError("--steps 20 must be refused")


def test_import_h3_48gb_does_not_load_mlx_core():
    """Importing h3_48gb must not pull the entire MLX stack.

    This prevents callers who use only checkpoint metadata (readable without MLX)
    from unexpectedly loading 55+ GB of models. Run in subprocess to avoid
    pollution from other tests.
    """
    code = "import sys; import h3_48gb; print('mlx.core' in sys.modules)"
    project_root = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(project_root),
    )
    assert result.returncode == 0, f"subprocess failed: {result.stderr}"
    is_loaded = result.stdout.strip() == "True"
    assert not is_loaded, "mlx.core must not be imported by importing h3_48gb"


def test_dir_returns_only_public_api():
    """dir(h3_48gb) must return each public name exactly once, no private names.

    This test ensures tab-completion and static tooling see only the intended API:
    - Every name in __all__ must be present
    - No duplicates (len(dir()) == len(set(dir())))
    - No private names (none starting with underscore)
    """
    import h3_48gb

    dir_output = dir(h3_48gb)

    # Invariant 1: No duplicates
    assert len(dir_output) == len(set(dir_output)), \
        f"dir(h3_48gb) has duplicates: {len(dir_output)} total, {len(set(dir_output))} unique"

    # Invariant 2: All __all__ names present
    missing = set(h3_48gb.__all__) - set(dir_output)
    assert not missing, f"dir(h3_48gb) missing __all__ names: {missing}"

    # Invariant 3: No private names
    private = [name for name in dir_output if name.startswith("_")]
    assert not private, f"dir(h3_48gb) leaks private names: {private}"

    # Invariant 4: All returned names are in __all__
    extra = set(dir_output) - set(h3_48gb.__all__)
    assert not extra, f"dir(h3_48gb) returns non-__all__ names: {extra}"


# -- list --------------------------------------------------------------------------------------

def test_list_reports_finished_runs(tmp_path):
    (tmp_path / "h3-a-512x512.json").write_text('{"tag": "a", "frames": 73}')
    rows = run_list(tmp_path)
    assert rows == [{"tag": "a", "frames": 73}]


def test_list_is_empty_for_an_outdir_with_no_finished_runs(tmp_path):
    assert run_list(tmp_path) == []


# -- status --------------------------------------------------------------------------------------

def test_status_json_lists_runs(tmp_path, capsys):
    """The machine contract: one JSON document, ok true, a runs array."""
    from tests.test_runs import write_checkpoint

    write_checkpoint(tmp_path / "r" / "checkpoints" / "h3-a.safetensors",
                     completed=3, total=7, started_at="2026-08-10T21:00:00",
                     completed_at_start=0, written_at="2026-08-10T21:30:00")

    assert main(["status", "--outdir", str(tmp_path), "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["runs"][0]["completed"] == 3


def test_status_refuses_a_missing_outdir(tmp_path, capsys):
    assert main(["status", "--outdir", str(tmp_path / "nope"), "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "outdir_not_found"


def test_status_orders_in_flight_before_stale(tmp_path, monkeypatch):
    """`run_status` sorts in-flight runs first, so a human scanning the report sees what is
    actually happening before what has died.

    The live run's directory name (`z-alive`) sorts *after* the dead one's (`a-dead`)
    alphabetically -- `scan` itself returns runs in path order, so if `run_status` ever drops its
    `.sort(...)` this fails instead of passing by the coincidence of matching names to path order.
    """
    import h3_48gb.runs as runs_mod
    from h3_48gb.cli import run_status
    from h3_48gb.runs import _parse
    from tests.test_runs import write_checkpoint

    write_checkpoint(tmp_path / "a-dead" / "checkpoints" / "h3-a.safetensors",
                     completed=2, total=7, started_at="2026-08-10T21:00:00",
                     completed_at_start=0, written_at="2026-08-10T21:20:00")
    write_checkpoint(tmp_path / "z-alive" / "checkpoints" / "h3-b.safetensors",
                     completed=2, total=7, started_at="2026-08-10T21:59:00",
                     completed_at_start=0, written_at="2026-08-10T22:00:00")

    monkeypatch.setattr(runs_mod, "_now", lambda: _parse("2026-08-10T22:00:30"))

    report = run_status(tmp_path)
    assert [r["state"] for r in report["runs"]] == ["in_flight", "stale"]
    assert [Path(r["outdir"]).name for r in report["runs"]] == ["z-alive", "a-dead"]


def test_format_status_reports_progress_for_an_in_flight_run():
    from h3_48gb.cli import format_status

    report = {
        "ok": True, "outdir": "/x",
        "runs": [{
            "outdir": "/x/r", "state": "in_flight", "completed": 3, "total": 7,
            "fraction": 3 / 7, "seconds_per_forward": 120.0, "eta_seconds": 480.0,
        }],
    }
    human = format_status(report)
    assert "r" in human
    assert "3/7" in human
    assert "43%" in human


def test_format_status_reports_a_stale_run_as_stopped():
    from h3_48gb.cli import format_status

    report = {
        "ok": True, "outdir": "/x",
        "runs": [{
            "outdir": "/x/r", "state": "stale", "completed": 3, "total": 7,
            "fraction": 3 / 7, "seconds_per_forward": None, "eta_seconds": None,
        }],
    }
    human = format_status(report)
    assert "остановлен" in human
    assert "3/7" in human


def test_format_status_says_nothing_is_running_when_the_report_is_empty():
    from h3_48gb.cli import format_status

    human = format_status({"ok": True, "outdir": "/x", "runs": []})
    assert "ничего не идёт" in human
    assert "/x" in human


def test_format_status_reports_an_unreadable_run_with_its_error():
    """`unreadable` is the whole reason `scan`'s protected `try` exists -- dropping it from the
    human report makes the command lie: "ничего не идёт" when a broken run is sitting right there,
    with a diagnosis already computed and thrown away.
    """
    from h3_48gb.cli import format_status

    report = {
        "ok": True, "outdir": "/x",
        "runs": [{
            "outdir": "/x/bad", "state": "unreadable", "completed": None, "total": None,
            "fraction": None, "seconds_per_forward": None, "eta_seconds": None,
            "age_seconds": None, "error": "ValueError: header length 999 exceeds the file",
        }],
    }
    human = format_status(report)
    assert "bad" in human
    assert "header length 999" in human
    assert "ничего не идёт" not in human


def test_format_status_reports_an_unknown_rate_run_without_a_verdict():
    """`unknown` states what is known (forwards done, time since the last write) and passes no
    judgment on whether the run is alive -- unlike `stale`, which asserts it is dead."""
    from h3_48gb.cli import format_status

    report = {
        "ok": True, "outdir": "/x",
        "runs": [{
            "outdir": "/x/old", "state": "unknown", "completed": 8, "total": 19,
            "fraction": 8 / 19, "age_seconds": 1004.76, "seconds_per_forward": None,
            "eta_seconds": None,
        }],
    }
    human = format_status(report)
    assert "8/19" in human
    assert "остановлен" not in human


def test_format_status_does_not_crash_when_total_is_unknown():
    """`eta_seconds` is `None` not only when the rate is unknown but also when `total` is -- a
    checkpoint whose `total_forwards` is missing or zero. Guarding on `seconds_per_forward` alone
    divides `None / 60` and crashes the whole report over one such file; guard on `eta_seconds`
    itself instead. `total=None` must also not render as the literal string "None"."""
    from h3_48gb.cli import format_status

    report = {
        "ok": True, "outdir": "/x",
        "runs": [{
            "outdir": "/x/r", "state": "in_flight", "completed": 8, "total": None,
            "fraction": None, "seconds_per_forward": 120.0, "eta_seconds": None,
        }],
    }
    human = format_status(report)  # must not raise
    assert "8/?" in human
    assert "None" not in human


def test_status_survives_a_checkpoint_with_a_zero_total(tmp_path, monkeypatch):
    """End-to-end: `total_forwards: 0` reaches `scan` as `total=None` (see `runs.py`'s `... or
    None`), while the rate can still be known -- exactly the shape `format_status` must not choke
    on.

    `_now` is pinned well inside the freshness window so this deterministically lands in
    `in_flight`, the branch that divides by `eta_seconds` -- without pinning, the outcome (and
    whether the crash this guards against is even exercised) would depend on wall-clock time
    relative to the hardcoded `written_at`.
    """
    import h3_48gb.runs as runs_mod
    from h3_48gb.cli import format_status, run_status
    from h3_48gb.runs import _parse
    from tests.test_runs import write_checkpoint

    write_checkpoint(tmp_path / "r" / "checkpoints" / "h3-a.safetensors",
                     completed=3, total=0, started_at="2026-08-10T21:00:00",
                     completed_at_start=0, written_at="2026-08-10T21:10:00")

    monkeypatch.setattr(runs_mod, "_now", lambda: _parse("2026-08-10T21:11:40"))  # 100 s later

    report = run_status(tmp_path)
    assert report["runs"][0]["state"] == "in_flight"
    format_status(report)  # must not raise


# -- doctor ------------------------------------------------------------------------------------

def test_doctor_reports_missing_components(tmp_path):
    report = run_doctor(tmp_path)
    assert report["ok"] is False
    assert "transformer" in report["missing"]


def test_doctor_reports_ok_when_everything_is_present(tmp_path):
    for name in ("transformer", "text_encoder", "video_vae", "audio_vae"):
        (tmp_path / name).mkdir()
    (tmp_path / "transformer" / "adaln_cache.safetensors").write_bytes(b"")
    report = run_doctor(tmp_path)
    assert report == {"ok": True, "checkpoint": str(tmp_path), "missing": []}


def test_doctor_reports_the_baked_adaln_cache_separately_from_the_component_dirs(tmp_path):
    """A converted checkpoint with every directory but no baked cache is still unusable."""
    for name in ("transformer", "text_encoder", "video_vae", "audio_vae"):
        (tmp_path / name).mkdir()
    report = run_doctor(tmp_path)
    assert report["ok"] is False
    assert report["missing"] == ["transformer/adaln_cache.safetensors"]


# -- watch ------------------------------------------------------------------------------------

def test_watch_exits_when_nothing_is_running(tmp_path, capsys):
    """It has to terminate on its own, or it cannot end a queue script."""
    assert main(["watch", "--outdir", str(tmp_path), "--interval", "0"]) == 0
    assert "ничего не идёт" in capsys.readouterr().out


def test_watch_continues_polling_on_unknown_state(tmp_path, monkeypatch):
    """watch loops while unknown state is present; sleeps prove it, not just format_status."""
    import time
    from tests.test_runs import write_checkpoint
    from h3_48gb.cli import run_watch

    run_dir = tmp_path / "r"
    write_checkpoint(run_dir / "checkpoints" / "h3-a.safetensors",
                     completed=3, total=7, written_at="2026-08-10T21:00:00")

    sleep_calls = []

    def stub_sleep(duration: float) -> None:
        sleep_calls.append(duration)
        if len(sleep_calls) == 1:
            # First sleep: delete the run so next iteration sees empty dir and exits cleanly
            import shutil
            shutil.rmtree(run_dir)

    monkeypatch.setattr(time, "sleep", stub_sleep)

    # interval > 0 so the loop can actually test continuation logic
    report = run_watch(tmp_path, interval=0.1)

    # Proof that unknown state caused continuation: sleep was called, meaning at least 2 iterations
    assert len(sleep_calls) >= 1, "watch must sleep before checking for more runs"
    # Proof that status was checked more than once: the final iteration sees empty dir
    assert len(report["runs"]) == 0, "run_watch must have iterated again after the unknown run vanished"


# -- resume ------------------------------------------------------------------------------------

class _StubPipe:
    """A pipe stub that supports both being called and the identity hook `run_resume` needs."""

    def checkpoint_identity_extra(self) -> dict:
        return {"weights": "stub-v1"}

    def __call__(self, **kwargs):
        return _StubResult()


def test_resume_fails_loudly_when_there_is_nothing_to_resume(tmp_path):
    spec = RunSpec(prompt="a cat", width=64, height=64, duration=1.0, steps=31, seed=0,
                   checkpoint=tmp_path, outdir=tmp_path, tag="t")
    try:
        run_resume(spec, pipeline_factory=lambda _: _StubPipe())
    except CliError as exc:
        assert exc.code == "checkpoint_not_found"
    else:
        raise AssertionError("resume without a matching checkpoint must fail, not start over silently")


def test_resume_continues_when_a_matching_checkpoint_exists(tmp_path):
    spec = RunSpec(prompt="a cat", width=64, height=64, duration=1.0, steps=31, seed=0,
                   checkpoint=tmp_path, outdir=tmp_path, tag="t")
    pipe = _StubPipe()
    path = _checkpoint_path_for(spec, pipe, tmp_path / "checkpoints")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"stand-in for a real checkpoint; the stub pipe never reads it")

    report = run_resume(spec, pipeline_factory=lambda _: pipe)
    assert report["tag"] == "t"


def test_resume_checkpoint_path_changes_with_the_request():
    """Two different requests must never resolve to the same checkpoint file."""
    spec_a = RunSpec(prompt="a cat", width=64, height=64, duration=1.0, steps=31, seed=0,
                     checkpoint=Path("/x"), outdir=Path("/x"), tag="t")
    spec_b = RunSpec(prompt="a dog", width=64, height=64, duration=1.0, steps=31, seed=0,
                     checkpoint=Path("/x"), outdir=Path("/x"), tag="t")
    pipe = _StubPipe()
    assert (_checkpoint_path_for(spec_a, pipe, Path("/ckpt"))
            != _checkpoint_path_for(spec_b, pipe, Path("/ckpt")))


def test_resume_checkpoint_path_changes_with_the_tag_alone():
    """Pins the fix: `tag` used to be invisible to `request_identity` (upstream's `__call__` has no
    such parameter, so it never reached `bound.arguments`), so two otherwise-identical specs
    differing only by `--tag` silently resolved to the *same* checkpoint file — a `--tag` that
    looked like it named a run isolated nothing. Same prompt/geometry/duration/steps/seed here;
    only the tag differs, so this must resolve to two different files, asserted directly.
    """
    spec_a = RunSpec(prompt="a cat", width=64, height=64, duration=1.0, steps=31, seed=0,
                     checkpoint=Path("/x"), outdir=Path("/x"), tag="a")
    spec_b = RunSpec(prompt="a cat", width=64, height=64, duration=1.0, steps=31, seed=0,
                     checkpoint=Path("/x"), outdir=Path("/x"), tag="b")
    pipe = _StubPipe()
    assert (_checkpoint_path_for(spec_a, pipe, Path("/ckpt"))
            != _checkpoint_path_for(spec_b, pipe, Path("/ckpt")))


def test_cli_and_checkpoint_module_agree_on_the_file_name():
    """`_checkpoint_path_for` (cli.py) must resolve to exactly what `_resolve_store`
    (checkpoint.py) resolves to for the same run.

    They are two independent reimplementations of one naming rule: the CLI needs the path *before*
    calling the pipeline (to tell "nothing to resume" from "resuming"), while the pipeline computes
    it internally on the way in. Every other resume test writes and reads through
    `_checkpoint_path_for` alone, so it would keep passing in perfect agreement with itself while
    `h3 resume` reported `checkpoint_not_found` for a checkpoint that was sitting right there.
    Bind them once, here.
    """
    import inspect

    from h3_48gb.checkpoint import _resolve_store, request_identity
    from minimax_h3_mlx.pipeline import MiniMaxH3Pipeline

    spec = RunSpec(prompt="a cat", width=64, height=64, duration=1.0, steps=31, seed=3,
                   checkpoint=Path("/x"), outdir=Path("/x"), tag="t")
    pipe = _StubPipe()
    ckpt_dir = Path("/ckpt")

    # The pipeline's own path: bind upstream's signature exactly as `CheckpointingPipeline.__call__`
    # does, from the same arguments `run_generate` passes to `pipe(...)`.
    bound = inspect.signature(MiniMaxH3Pipeline.__call__).bind(
        pipe, prompt=spec.prompt, duration_seconds=spec.duration,
        num_inference_steps=spec.steps, seed=spec.seed, height=spec.height, width=spec.width,
    )
    bound.apply_defaults()
    identity = request_identity(dict(bound.arguments), pipe.checkpoint_identity_extra(), tag=spec.tag)
    from_pipeline = _resolve_store({"checkpoint_dir": str(ckpt_dir)}, identity).path

    assert _checkpoint_path_for(spec, pipe, ckpt_dir) == from_pipeline


def test_cli_and_checkpoint_module_agree_on_the_file_name_with_a_keyframe(tmp_path):
    """The conditioned counterpart of `test_cli_and_checkpoint_module_agree_on_the_file_name`.

    That test's hand-rolled `bound(...)` never passes `images`/`keyframe_anchors`, so it cannot
    tell a correct keyframe binding in `_checkpoint_path_for` from a broken one — both look
    identical to it when no image is involved (unconditioned `_checkpoint_path_for` and the
    unconditioned oracle here would agree even if `_checkpoint_path_for` bound the wrong parameter
    name, or dropped keyframe_anchors, as long as neither is ever exercised). This binds a real
    `image=` through `load_keyframes` into *both* resolutions — `_checkpoint_path_for`'s own
    binding, and this test's independent oracle built from `_resolve_store`, the function
    `CheckpointingPipeline.__call__` actually calls internally — so a keyframe-binding mistake in
    `_checkpoint_path_for` shows up as a mismatch here even though the two tests above
    (`test_a_keyframe_changes_the_checkpoint_identity`,
    `test_different_keyframes_give_different_checkpoints`) would not catch it: they only compare
    `_checkpoint_path_for` against itself, so a binding mistake shared by both calls would pass
    them silently.
    """
    import inspect

    from h3_48gb.checkpoint import _resolve_store, request_identity
    from h3_48gb.cli import load_keyframes
    from minimax_h3_mlx.pipeline import MiniMaxH3Pipeline

    spec = _spec(tmp_path, image=_png(tmp_path / "a.png"))
    pipe = _StubPipe()
    ckpt_dir = tmp_path / "checkpoints"

    # The pipeline's own path: bind upstream's signature exactly as `CheckpointingPipeline.__call__`
    # does, from the same `images`/`keyframe_anchors` `run_generate` passes to `pipe(...)`.
    images, keyframe_anchors = load_keyframes(spec)
    bound = inspect.signature(MiniMaxH3Pipeline.__call__).bind(
        pipe, prompt=spec.prompt, duration_seconds=spec.duration,
        num_inference_steps=spec.steps, seed=spec.seed, height=spec.height, width=spec.width,
        images=images or None, keyframe_anchors=keyframe_anchors,
    )
    bound.apply_defaults()
    identity = request_identity(dict(bound.arguments), pipe.checkpoint_identity_extra(), tag=spec.tag)
    from_pipeline = _resolve_store({"checkpoint_dir": str(ckpt_dir)}, identity).path

    assert _checkpoint_path_for(spec, pipe, ckpt_dir) == from_pipeline


# -- preview and checkpoint control ---------------------------------------------------------------

def test_generate_exposes_preview_and_checkpoint_flags():
    args = build_parser().parse_args(["generate", "a cat"])
    assert args.preview_every == 5, "previews cost 0.125 s with TAE; they are on by default"
    assert args.preview_decoder == "tae", "at 49.3 s each the real VAE cannot be the on-by-default"
    assert args.preview_stem is None and args.checkpoint_dir is None
    assert args.restart is False and args.no_checkpoint is False


def test_preview_arguments_reach_the_pipeline(tmp_path):
    """`--preview-every`/`--preview-stem` were parsed by nothing and reached nothing before this."""
    seen = {}

    def recording_pipe(**kwargs):
        seen.update(kwargs)
        return _StubResult()

    args = build_parser().parse_args(
        ["generate", "a cat", "--width", "64", "--height", "64", "--tag", "t",
         "--outdir", str(tmp_path), "--preview-every", "3"])
    run_generate(spec_from_args(args), pipeline_factory=lambda _: recording_pipe,
                 save_mp4_fn=lambda *a: None, save_wav_fn=lambda *a: None)

    assert seen["preview_every"] == 3
    # Defaults to the run's own output stem, so previews land beside the mp4 they preview.
    assert seen["preview_stem"] == str(tmp_path / "h3-t-64x64")


def test_preview_stem_can_be_pointed_elsewhere(tmp_path):
    seen = {}

    def recording_pipe(**kwargs):
        seen.update(kwargs)
        return _StubResult()

    args = build_parser().parse_args(
        ["generate", "a cat", "--width", "64", "--height", "64", "--tag", "t",
         "--outdir", str(tmp_path), "--preview-every", "2",
         "--preview-stem", str(tmp_path / "elsewhere" / "peek")])
    run_generate(spec_from_args(args), pipeline_factory=lambda _: recording_pipe,
                 save_mp4_fn=lambda *a: None, save_wav_fn=lambda *a: None)
    assert seen["preview_stem"] == str(tmp_path / "elsewhere" / "peek")


def test_previews_disabled_explicitly_pass_no_stem(tmp_path):
    """`--preview-every 0` must also clear the stem: the pipeline refuses a stem it will never use.

    Previews are on by default now, so this is the explicit opt-*out* path rather than the
    default one — but the invariant it guards is unchanged.
    """
    seen = {}

    def recording_pipe(**kwargs):
        seen.update(kwargs)
        return _StubResult()

    args = build_parser().parse_args(
        ["generate", "a cat", "--width", "64", "--height", "64", "--preview-every", "0",
         "--outdir", str(tmp_path)])
    run_generate(spec_from_args(args), pipeline_factory=lambda _: recording_pipe,
                 save_mp4_fn=lambda *a: None, save_wav_fn=lambda *a: None)
    assert seen["preview_every"] == 0 and seen["preview_stem"] is None


def test_negative_preview_interval_is_refused_with_a_code():
    args = build_parser().parse_args(["generate", "a cat", "--preview-every", "-1"])
    try:
        spec_from_args(args)
    except CliError as exc:
        assert exc.code == "preview_interval_negative"
    else:
        raise AssertionError("a negative preview cadence must be refused, not passed through")


def test_previews_are_on_by_default_and_use_tae(tmp_path):
    """The two defaults are one decision: previews can only be on because TAE made them cheap.

    At the real VAE's 49.3 s per preview, six of them would add five minutes to every run. At
    TAE's 0.125 s they add 0.75 s. Turning previews on while leaving `vae` as the decoder would
    be the worst of both.
    """
    spec = spec_from_args(build_parser().parse_args(
        ["generate", "a cat", "--outdir", str(tmp_path)]))
    assert (spec.preview_every, spec.preview_decoder) == (5, "tae")


def test_the_real_vae_is_still_reachable_for_an_exact_preview(tmp_path):
    """TAE is an approximation for watching progress; a preview that must be exact needs the VAE."""
    spec = spec_from_args(build_parser().parse_args(
        ["generate", "a cat", "--preview-decoder", "vae", "--outdir", str(tmp_path)]))
    assert spec.preview_decoder == "vae"


def test_the_preview_decoder_can_be_chosen(tmp_path):
    spec = spec_from_args(build_parser().parse_args(
        ["generate", "a cat", "--preview-decoder", "tae", "--outdir", str(tmp_path)]))
    assert spec.preview_decoder == "tae"


def test_an_unknown_preview_decoder_is_refused_by_the_parser(tmp_path):
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            ["generate", "a cat", "--preview-decoder", "taa", "--outdir", str(tmp_path)])


def test_the_preview_decoder_reaches_the_pipeline(tmp_path):
    """The flag is worthless if it stops at the RunSpec."""
    seen = {}

    def factory(_checkpoint):
        def pipe(**kwargs):
            seen.update(kwargs)
            return _StubResult()
        return pipe

    spec = _spec(tmp_path, preview_every=2, preview_decoder="tae")
    run_generate(spec, pipeline_factory=factory)
    assert seen.get("preview_decoder") == "tae"


# -- keyframe conditioning ---------------------------------------------------------------------

def _spec(tmp_path, **overrides):
    base = dict(prompt="a cat", width=64, height=64, duration=1.0, steps=31, seed=0,
                checkpoint=tmp_path, outdir=tmp_path, tag="t")
    base.update(overrides)
    return RunSpec(**base)


def _png(path, size=(64, 64), colour=(200, 30, 30)):
    from PIL import Image
    Image.new("RGB", size, colour).save(path)
    return path


def test_end_image_without_image_is_refused(tmp_path):
    last = tmp_path / "last.png"
    last.write_bytes(b"not really a png, never opened")
    with pytest.raises(CliError) as excinfo:
        _spec(tmp_path, end_image=last)
    assert excinfo.value.code == "end_image_without_image"


def test_a_missing_keyframe_is_refused_by_path(tmp_path):
    with pytest.raises(CliError) as excinfo:
        _spec(tmp_path, image=tmp_path / "absent.png")
    assert excinfo.value.code == "image_not_found"
    assert "absent.png" in excinfo.value.message


def test_both_keyframes_present_is_accepted(tmp_path):
    first, last = tmp_path / "a.png", tmp_path / "b.png"
    first.write_bytes(b"x")
    last.write_bytes(b"y")
    spec = _spec(tmp_path, image=first, end_image=last)
    assert (spec.image, spec.end_image) == (first, last)


def test_parser_accepts_the_two_flags(tmp_path):
    args = build_parser().parse_args(
        ["generate", "a cat", "--image", str(tmp_path / "a.png"),
         "--end-image", str(tmp_path / "b.png")]
    )
    assert args.image == tmp_path / "a.png"
    assert args.end_image == tmp_path / "b.png"


def test_one_image_anchors_the_first_frame(tmp_path):
    from h3_48gb.cli import load_keyframes

    images, anchors = load_keyframes(_spec(tmp_path, image=_png(tmp_path / "a.png")))
    assert anchors == ("first",)
    assert len(images) == 1


def test_two_images_anchor_both_ends(tmp_path):
    from h3_48gb.cli import load_keyframes

    spec = _spec(tmp_path, image=_png(tmp_path / "a.png"),
                 end_image=_png(tmp_path / "b.png", colour=(30, 30, 200)))
    images, anchors = load_keyframes(spec)
    assert anchors == ("first", "last")
    assert len(images) == 2


def test_no_image_means_no_conditioning(tmp_path):
    from h3_48gb.cli import load_keyframes

    assert load_keyframes(_spec(tmp_path)) == ([], ())


def test_exif_rotation_is_applied(tmp_path):
    """A phone photo carries its rotation in EXIF. Ignoring it conditions the run on a
    differently-oriented frame than the user saw, silently.

    Checked by content, not by size: keyframes now come back fitted to the canvas, so the size
    after loading says nothing about whether the tag was honoured.
    """
    from PIL import Image
    from h3_48gb.cli import load_keyframes

    path = tmp_path / "rotated.jpg"
    exif = Image.Exif()
    exif[274] = 6  # Orientation: rotate 90 degrees clockwise
    # Left half red, right half blue. Rotating 90° CW puts the left half on top.
    source = Image.new("RGB", (64, 32), (0, 0, 220))
    source.paste(Image.new("RGB", (32, 32), (220, 0, 0)), (0, 0))
    source.save(path, exif=exif)

    images, _ = load_keyframes(_spec(tmp_path, image=path))
    frame = np.asarray(images[0])
    top, bottom = frame[:16].mean(axis=(0, 1)), frame[-16:].mean(axis=(0, 1))
    assert top[0] > top[2], f"the red half should be on top after rotation, got {top}"
    assert bottom[2] > bottom[0], f"the blue half should be at the bottom, got {bottom}"


def test_keyframes_passed_to_pipeline_with_no_images(tmp_path):
    """Verify run_generate wires keyframes to the pipeline call: empty conditioning case."""
    seen = {}

    def recording_pipe(**kwargs):
        seen.update(kwargs)
        return _StubResult()

    spec = _spec(tmp_path)  # No image or end_image
    run_generate(spec, pipeline_factory=lambda _: recording_pipe,
                 save_mp4_fn=lambda *a: None, save_wav_fn=lambda *a: None)
    assert seen["images"] is None, "empty conditioning must pass None, not []"
    assert seen["keyframe_anchors"] == ()


def test_keyframes_passed_to_pipeline_with_one_image(tmp_path):
    """Verify run_generate wires keyframes to the pipeline call: single-image case."""
    seen = {}

    def recording_pipe(**kwargs):
        seen.update(kwargs)
        return _StubResult()

    spec = _spec(tmp_path, image=_png(tmp_path / "a.png"))
    run_generate(spec, pipeline_factory=lambda _: recording_pipe,
                 save_mp4_fn=lambda *a: None, save_wav_fn=lambda *a: None)
    assert len(seen["images"]) == 1
    assert seen["keyframe_anchors"] == ("first",)


def test_keyframes_passed_to_pipeline_with_two_images(tmp_path):
    """Verify run_generate wires keyframes to the pipeline call: both-ends case."""
    seen = {}

    def recording_pipe(**kwargs):
        seen.update(kwargs)
        return _StubResult()

    spec = _spec(tmp_path, image=_png(tmp_path / "a.png"),
                 end_image=_png(tmp_path / "b.png", colour=(30, 30, 200)))
    run_generate(spec, pipeline_factory=lambda _: recording_pipe,
                 save_mp4_fn=lambda *a: None, save_wav_fn=lambda *a: None)
    assert len(seen["images"]) == 2
    assert seen["keyframe_anchors"] == ("first", "last")


def test_a_keyframe_changes_the_checkpoint_identity(tmp_path):
    """Resuming a conditioned run from an unconditioned checkpoint would restart the clip
    from different latents than the ones it was written for."""
    plain = _spec(tmp_path)
    conditioned = _spec(tmp_path, image=_png(tmp_path / "a.png"))
    pipe = _StubPipe()
    ckpt_dir = tmp_path / "checkpoints"
    assert (_checkpoint_path_for(plain, pipe, ckpt_dir)
            != _checkpoint_path_for(conditioned, pipe, ckpt_dir))


def test_different_keyframes_give_different_checkpoints(tmp_path):
    red = _spec(tmp_path, image=_png(tmp_path / "red.png", colour=(200, 30, 30)))
    blue = _spec(tmp_path, image=_png(tmp_path / "blue.png", colour=(30, 30, 200)))
    pipe = _StubPipe()
    ckpt_dir = tmp_path / "checkpoints"
    assert (_checkpoint_path_for(red, pipe, ckpt_dir)
            != _checkpoint_path_for(blue, pipe, ckpt_dir))


def test_renaming_a_keyframe_keeps_the_same_checkpoint(tmp_path):
    """The digest is over content, not path — a renamed file is the same keyframe.

    `_checkpoint_path_for` reads the keyframe (via `load_keyframes`) at the moment it computes the
    identity, so `path_a` has to be captured before the rename — exactly as it would be in
    practice: a `generate` run digests the file while it still exists at its then-current path, a
    later `resume` after the operator renamed it digests the same bytes under the new name.
    """
    original = _png(tmp_path / "a.png")
    spec_a = _spec(tmp_path, image=original)
    pipe = _StubPipe()
    ckpt_dir = tmp_path / "checkpoints"
    path_a = _checkpoint_path_for(spec_a, pipe, ckpt_dir)

    renamed = tmp_path / "b.png"
    original.rename(renamed)
    spec_b = _spec(tmp_path, image=renamed)
    path_b = _checkpoint_path_for(spec_b, pipe, ckpt_dir)

    assert path_a == path_b


def test_checkpoint_dir_overrides_the_default_location(tmp_path):
    seen = {}

    def recording_pipe(**kwargs):
        seen.update(kwargs)
        return _StubResult()

    elsewhere = tmp_path / "ckpts"
    args = build_parser().parse_args(
        ["generate", "a cat", "--width", "64", "--height", "64", "--outdir", str(tmp_path),
         "--checkpoint-dir", str(elsewhere)])
    spec = spec_from_args(args)
    assert spec.resume_checkpoint_dir() == elsewhere
    run_generate(spec, pipeline_factory=lambda _: recording_pipe,
                 save_mp4_fn=lambda *a: None, save_wav_fn=lambda *a: None)
    assert seen["checkpoint_dir"] == str(elsewhere)


def test_no_checkpoint_turns_checkpointing_off(tmp_path):
    """`checkpoint_dir=None` is what makes `CheckpointingPipeline.__call__` fall through
    to upstream's untouched `__call__` — so this must be `None`, not a directory that is unused."""
    seen = {}

    def recording_pipe(**kwargs):
        seen.update(kwargs)
        return _StubResult()

    args = build_parser().parse_args(
        ["generate", "a cat", "--width", "64", "--height", "64", "--outdir", str(tmp_path),
         "--no-checkpoint"])
    spec = spec_from_args(args)
    assert spec.resume_checkpoint_dir() is None
    run_generate(spec, pipeline_factory=lambda _: recording_pipe,
                 save_mp4_fn=lambda *a: None, save_wav_fn=lambda *a: None)
    assert seen["checkpoint_dir"] is None


def test_restart_disables_resumption(tmp_path):
    """The escape hatch from `checkpoint_mismatch`: keep checkpointing, ignore what is on disk."""
    seen = {}

    def recording_pipe(**kwargs):
        seen.update(kwargs)
        return _StubResult()

    import unittest.mock as mock

    argv = ["generate", "a cat", "--width", "64", "--height", "64", "--outdir", str(tmp_path),
            "--restart"]
    with mock.patch("h3_48gb.cli._default_pipeline_factory", return_value=recording_pipe), \
         mock.patch("h3_48gb.cli.run_generate", wraps=run_generate) as spy:
        main(argv + ["--json"])
    assert spy.call_args.kwargs["resume"] is False


def test_restart_is_named_in_the_mismatch_refusal(tmp_path):
    """A user who hits `checkpoint_mismatch` cannot compute the `h3-{digest}.safetensors` filename
    they are being told about, so the message has to name the flag that recovers from it."""
    from h3_48gb.checkpoint import CheckpointMismatch

    def exploding_pipe(**kwargs):
        raise CheckpointMismatch("belongs to a different run")

    spec = RunSpec(prompt="x", width=64, height=64, duration=1.0, steps=31, seed=0,
                   checkpoint=tmp_path, outdir=tmp_path, tag="t")
    try:
        run_generate(spec, pipeline_factory=lambda _: exploding_pipe)
    except CliError as exc:
        assert "--restart" in str(exc)
    else:
        raise AssertionError("expected a checkpoint_mismatch refusal")


# -- machine-readable failures ------------------------------------------------------------------

def test_checkpoint_mismatch_surfaces_as_a_cli_error_not_a_raw_exception(tmp_path):
    from h3_48gb.checkpoint import CheckpointMismatch

    def exploding_pipe(**kwargs):
        raise CheckpointMismatch("this checkpoint belongs to a different run")

    spec = RunSpec(prompt="x", width=64, height=64, duration=1.0, steps=31, seed=0,
                   checkpoint=tmp_path, outdir=tmp_path, tag="t")
    try:
        run_generate(spec, pipeline_factory=lambda _: exploding_pipe)
    except CliError as exc:
        assert exc.code == "checkpoint_mismatch"
    else:
        raise AssertionError("a CheckpointMismatch must surface as a machine-readable CliError")


def test_checkpoint_corrupt_surfaces_as_a_cli_error_not_a_raw_exception(tmp_path):
    from h3_48gb.checkpoint import CheckpointCorrupt

    def exploding_pipe(**kwargs):
        raise CheckpointCorrupt("could not be read")

    spec = RunSpec(prompt="x", width=64, height=64, duration=1.0, steps=31, seed=0,
                   checkpoint=tmp_path, outdir=tmp_path, tag="t")
    try:
        run_generate(spec, pipeline_factory=lambda _: exploding_pipe)
    except CliError as exc:
        assert exc.code == "checkpoint_corrupt"
    else:
        raise AssertionError("a CheckpointCorrupt must surface as a machine-readable CliError")


def test_main_emits_a_json_error_on_stdout_with_nonzero_exit(capsys):
    code = main(["generate", "a cat", "--height", "433", "--json"])
    assert code == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "geometry_not_multiple_of_32"
    assert payload["error"]["detail"] == {"height": 433}


def test_main_prints_a_human_sentence_to_stderr_without_json(capsys):
    code = main(["generate", "a cat", "--height", "433"])
    assert code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "multiple of 32" in captured.err


def test_main_list_json(tmp_path, capsys):
    (tmp_path / "h3-a-512x512.json").write_text('{"tag": "a", "frames": 73}')
    code = main(["list", "--outdir", str(tmp_path), "--json"])
    assert code == 0
    assert json.loads(capsys.readouterr().out) == [{"tag": "a", "frames": 73}]


def test_main_doctor_json_reports_failure_with_nonzero_exit(tmp_path, capsys):
    code = main(["doctor", "--checkpoint", str(tmp_path), "--json"])
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False


def test_error_codes_are_documented_in_one_place():
    """Codes are part of the public contract; this is the "list them in one place" the task asked for."""
    from h3_48gb.cli import ERROR_CODES

    for code in ("geometry_not_multiple_of_32", "schedule_not_baked", "checkpoint_not_found",
                 "checkpoint_mismatch", "checkpoint_corrupt"):
        assert code in ERROR_CODES


# -- verbose: the --json stdout contract must survive a chatty pipeline -------------------------

def _chatty_pipeline_factory(checkpoint, verbose=True, **kwargs):
    """Stands in for `_default_pipeline_factory`, printing exactly what the real one and the
    checkpoint writer do when `verbose` is left on: `LazyMiniMaxH3Pipeline.from_pretrained` prints
    while loading configs, and `ResumableRun._write` prints its "checkpoint: N/M steps" line on
    every checkpointed step. Every other stub pipe in this file is silent, which is exactly why the
    suite did not catch `--json` emitting unparseable stdout on a real (non-erroring) run.
    """
    if verbose:
        print(f"loading MiniMax-H3 configs from {checkpoint} (weights are deferred)")

    def pipe(**kwargs):
        if kwargs.get("verbose", True):
            print("  checkpoint: 1/30 steps, 12.3 MB -> somewhere.safetensors")
        return _StubResult()

    return pipe


def test_main_json_output_stays_parseable_with_a_chatty_pipeline(tmp_path, monkeypatch, capsys):
    """Regression for the reviewer-reproduced bug: progress lines interleaved with the JSON report
    made `json.loads(stdout)` raise `JSONDecodeError`. `verbose` must reach both writers."""
    monkeypatch.setattr("h3_48gb.cli._default_pipeline_factory", _chatty_pipeline_factory)
    monkeypatch.setattr("minimax_h3_mlx.media.save_mp4", lambda *a, **kw: None)
    monkeypatch.setattr("minimax_h3_mlx.media.save_wav", lambda *a, **kw: None)

    code = main(["generate", "a cat", "--width", "64", "--height", "64", "--duration", "1",
                 "--outdir", str(tmp_path), "--json"])
    assert code == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)  # must not raise JSONDecodeError
    assert payload["tag"] == "run"


def test_main_human_mode_still_shows_pipeline_progress(tmp_path, monkeypatch, capsys):
    """The fix for the bug above must not go too far and silence progress that was never the
    problem: a five-hour render without --json still needs to show it is doing something."""
    monkeypatch.setattr("h3_48gb.cli._default_pipeline_factory", _chatty_pipeline_factory)
    monkeypatch.setattr("minimax_h3_mlx.media.save_mp4", lambda *a, **kw: None)
    monkeypatch.setattr("minimax_h3_mlx.media.save_wav", lambda *a, **kw: None)

    code = main(["generate", "a cat", "--width", "64", "--height", "64", "--duration", "1",
                 "--outdir", str(tmp_path)])
    assert code == 0
    captured = capsys.readouterr()
    assert "loading MiniMax-H3 configs" in captured.out
    assert "checkpoint: 1/30 steps" in captured.out


# -- main's last-resort JSON safety net -----------------------------------------------------------

def test_main_internal_error_is_valid_json_with_nonzero_exit(tmp_path, monkeypatch, capsys):
    """An exception `CliError` was never meant to classify (a bug, not a validation refusal) must
    still leave --json's stdout contract intact rather than dumping a bare traceback onto it."""
    def exploding_list(outdir):
        raise RuntimeError("disk exploded")

    monkeypatch.setattr("h3_48gb.cli.run_list", exploding_list)

    code = main(["list", "--outdir", str(tmp_path), "--json"])
    assert code == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload == {
        "ok": False,
        "error": {"code": "internal_error", "message": "disk exploded", "detail": {}},
    }


def test_main_internal_error_still_raises_in_human_mode(tmp_path, monkeypatch):
    """Human mode must not swallow a real bug behind the tidy JSON envelope."""
    def exploding_list(outdir):
        raise RuntimeError("disk exploded")

    monkeypatch.setattr("h3_48gb.cli.run_list", exploding_list)

    try:
        main(["list", "--outdir", str(tmp_path)])
    except RuntimeError as exc:
        assert "disk exploded" in str(exc)
    else:
        raise AssertionError("an unclassified exception must still surface in human mode")


# -- the vendored upstream must carry this fork's keyframe patch ---------------------------------

def test_the_patch_detector_agrees_with_the_patch_file():
    """The marker must be the line the patch actually removes, or the guard rots silently.

    Read from `patches/`, not from a copy: if someone rewrites the patch, this fails rather than
    letting the detector keep looking for an expression that no longer means anything.
    """
    from h3_48gb.text_encoder import UNPATCHED_SCATTER

    patch = (Path(__file__).resolve().parent.parent
             / "patches/0001-keyframe-masked-scatter.patch").read_text()
    removed = [line[1:] for line in patch.splitlines()
               if line.startswith("-") and not line.startswith("---")]
    assert any(UNPATCHED_SCATTER in line for line in removed), (
        f"{UNPATCHED_SCATTER!r} is not among the lines the patch removes: {removed}")


def test_the_vendored_checkout_is_patched():
    """Not a unit test of the detector — a statement about this working tree."""
    from h3_48gb.text_encoder import keyframe_scatter_patch_applied

    assert keyframe_scatter_patch_applied(), (
        "upstream/ is unpatched; run "
        "`git -C upstream apply ../patches/0001-keyframe-masked-scatter.patch`")


def test_a_keyframe_on_an_unpatched_checkout_is_refused_before_any_weight_loads(tmp_path,
                                                                                monkeypatch):
    from h3_48gb import cli, text_encoder

    image = tmp_path / "first.png"
    Image.new("RGB", (64, 64), (200, 40, 40)).save(image)
    monkeypatch.setattr(text_encoder, "keyframe_scatter_patch_applied", lambda: False)

    with pytest.raises(CliError) as excinfo:
        cli.load_keyframes(_spec(tmp_path, image=image))
    assert excinfo.value.code == "upstream_patch_missing"
    assert "0001-keyframe-masked-scatter.patch" in excinfo.value.message


# -- the keyframe is the geometry anchor ---------------------------------------------------------

def _canvas(tmp_path, argv):
    from h3_48gb.cli import spec_from_args
    spec = spec_from_args(build_parser().parse_args(
        argv + ["--outdir", str(tmp_path), "--checkpoint", str(tmp_path)]))
    return spec.width, spec.height


def test_a_text_only_run_gets_the_default_canvas(tmp_path):
    """Not the released 1344x768: that is 31 min of diffusion for 2.4 s and nobody asked for it.

    The default keeps the released 16:9 shape and drops to a canvas that still renders faces —
    the 512-vs-768 measurement over three seeds is in `docs/RESULTS.md`. Pinned literally rather
    than against `DEFAULT_CANVAS` so that changing the constant has to change a test too.
    """
    assert _canvas(tmp_path, ["generate", "a cat"]) == (896, 512)
    width, height = _canvas(tmp_path, ["generate", "a cat"])
    assert width % 32 == 0 and height % 32 == 0, "the port cannot pack a canvas off the 32 grid"
    assert abs(width / height - 1344 / 768) < 1e-9, "the default must keep the released aspect"


def test_the_canvas_follows_the_keyframe(tmp_path):
    """The first keyframe is *stretched* onto the canvas, so a wrong canvas deforms the clip.

    The reference resolves geometry from the first frame's aspect; before this, a portrait photo
    was silently squashed into 16:9.
    """
    portrait = tmp_path / "portrait.png"
    Image.new("RGB", (896, 1152), (200, 40, 40)).save(portrait)
    width, height = _canvas(tmp_path, ["generate", "a cat", "--image", str(portrait)])
    assert width < height, f"a portrait keyframe produced a {width}x{height} canvas"
    assert abs((width / height) - (896 / 1152)) < 0.02


def test_an_explicit_canvas_still_wins_over_the_keyframe(tmp_path):
    portrait = tmp_path / "portrait.png"
    Image.new("RGB", (896, 1152), (200, 40, 40)).save(portrait)
    assert _canvas(tmp_path, ["generate", "a cat", "--image", str(portrait),
                              "--width", "448", "--height", "576"]) == (448, 576)


def test_exif_orientation_decides_the_canvas_too(tmp_path):
    """A camera marks rotation in a tag; unread, a portrait photo reports itself as landscape —
    and would pick the very canvas that deforms it."""
    rotated = tmp_path / "rotated.jpg"
    # 6 = rotate 90° CW on display: stored 1152x896, shown 896x1152.
    exif = Image.Exif()
    exif[274] = 6
    Image.new("RGB", (1152, 896), (200, 40, 40)).save(rotated, exif=exif)

    width, height = _canvas(tmp_path, ["generate", "a cat", "--image", str(rotated)])
    assert width < height, f"the EXIF tag was ignored: got a {width}x{height} canvas"


def test_the_pipeline_conditions_on_exactly_the_frame_the_digest_was_taken_over(tmp_path):
    """The checkpoint's identity and the clip's conditioning must describe the same picture.

    They are computed in different places — the digest from `load_keyframes` in the CLI, the
    conditioning from whatever reaches the pipeline — so a transform applied on one side only
    splits them. That happened: with the canvas fit living in `LazyMiniMaxH3Pipeline.__call__`,
    `h3 resume --image photo.jpg` hashed the raw frame, the generate run had hashed the fitted
    one, and resume reported `checkpoint_not_found` for a checkpoint sitting right there.

    The keyframe here is deliberately a different size and aspect from the canvas: at 64x64 into
    64x64 the fit is a no-op and this test could not fail.
    """
    import functools
    import inspect

    from h3_48gb.checkpoint import _image_digest
    from h3_48gb.cli import load_keyframes
    from h3_48gb.pipeline import LazyMiniMaxH3Pipeline
    from minimax_h3_mlx.pipeline import MiniMaxH3Pipeline

    path = tmp_path / "wide.png"
    Image.new("RGB", (128, 96), (200, 30, 30)).save(path)
    spec = _spec(tmp_path, image=path)
    images, anchors = load_keyframes(spec)
    assert images[0].size == (spec.width, spec.height), "load_keyframes must fit to the canvas"

    class Config:
        sigma_shift_video = 12.0
        sigma_shift_audio = 3.0

    pipe = LazyMiniMaxH3Pipeline(object(), object(), object(), object(), Config(), verbose=False)
    pipe.supported_num_inference_steps = lambda: None
    seen = {}

    original = MiniMaxH3Pipeline.__call__

    @functools.wraps(original)
    def spy(self, *args, **kwargs):
        bound = inspect.signature(original).bind(self, *args, **kwargs)
        bound.apply_defaults()
        seen["images"] = bound.arguments["images"]
        return "ok"

    MiniMaxH3Pipeline.__call__ = spy
    try:
        pipe(prompt=spec.prompt, duration_seconds=spec.duration, num_inference_steps=spec.steps,
             seed=spec.seed, height=spec.height, width=spec.width,
             images=images, keyframe_anchors=anchors)
    finally:
        MiniMaxH3Pipeline.__call__ = original

    assert _image_digest(seen["images"][0]) == _image_digest(images[0]), (
        "the frame the pipeline conditions on differs from the one the digest was taken over")


def test_a_missing_keyframe_is_refused_by_code_not_by_traceback(tmp_path):
    """`resolve_canvas` opens the file before `RunSpec` validates it, so the refusal is its job."""
    with pytest.raises(CliError) as excinfo:
        spec_from_args(build_parser().parse_args(
            ["generate", "a cat", "--image", str(tmp_path / "absent.png"),
             "--outdir", str(tmp_path)]))
    assert excinfo.value.code == "image_not_found"


def test_an_undecodable_keyframe_is_refused_by_code(tmp_path):
    path = tmp_path / "broken.png"
    path.write_bytes(b"this is not a PNG")
    with pytest.raises(CliError) as excinfo:
        spec_from_args(build_parser().parse_args(
            ["generate", "a cat", "--image", str(path), "--outdir", str(tmp_path)]))
    assert excinfo.value.code == "image_unreadable"


def test_an_extreme_aspect_keyframe_is_refused_with_advice(tmp_path):
    """The model supports 1:4..4:1. A 10:1 panorama must say so, not raise ValueError."""
    path = tmp_path / "panorama.png"
    Image.new("RGB", (2000, 200), (200, 30, 30)).save(path)
    with pytest.raises(CliError) as excinfo:
        spec_from_args(build_parser().parse_args(
            ["generate", "a cat", "--image", str(path), "--outdir", str(tmp_path)]))
    assert excinfo.value.code == "image_aspect_unsupported"
    assert "--width" in excinfo.value.message, "the message must name the way out"


def test_half_a_canvas_with_a_keyframe_is_refused(tmp_path):
    """`--width` alone against a 3:2 photo used to pair it with the derived height, giving a
    canvas of neither aspect and stretching the frame into it silently."""
    path = tmp_path / "wide.png"
    Image.new("RGB", (1536, 1024), (200, 30, 30)).save(path)
    with pytest.raises(CliError) as excinfo:
        spec_from_args(build_parser().parse_args(
            ["generate", "a cat", "--image", str(path), "--width", "640",
             "--outdir", str(tmp_path)]))
    assert excinfo.value.code == "partial_canvas_with_image"


def test_half_a_canvas_without_a_keyframe_still_works(tmp_path):
    """Text-only runs keep the old behaviour: one axis given, the other defaults."""
    spec = spec_from_args(build_parser().parse_args(
        ["generate", "a cat", "--width", "640", "--outdir", str(tmp_path)]))
    assert (spec.width, spec.height) == (640, 512)


def test_the_patch_detector_can_actually_tell_the_two_apart():
    """Without this, replacing the detector's body with `return True` passed the whole suite.

    The unpatched line is taken from the patch file rather than retyped, so the two cannot drift.
    """
    from h3_48gb.text_encoder import keyframe_scatter_patch_applied

    patch = (Path(__file__).resolve().parent.parent
             / "patches/0001-keyframe-masked-scatter.patch").read_text()
    removed = "\n".join(line[1:] for line in patch.splitlines()
                        if line.startswith("-") and not line.startswith("---"))
    added = "\n".join(line[1:] for line in patch.splitlines()
                      if line.startswith("+") and not line.startswith("+++"))

    assert not keyframe_scatter_patch_applied(source=removed), (
        "the lines the patch removes must read as unpatched")
    assert keyframe_scatter_patch_applied(source=added), (
        "the lines the patch adds must read as patched — they quote the old expression in a "
        "comment, which is exactly the case the comment-stripping exists for")
    assert keyframe_scatter_patch_applied(source="def encode(self):\n    return 1\n"), (
        "unrelated source has no marker and must read as patched")


def test_an_undecodable_keyframe_is_refused_on_every_path(tmp_path):
    """`resolve_canvas` only decodes when it has to derive the canvas, and only `--image`.

    With an explicit canvas, or with `--end-image`, `load_keyframes` is where the file is first
    opened — and it used to let PIL's exception through as an `internal_error` traceback.
    """
    from h3_48gb.cli import load_keyframes

    broken = tmp_path / "broken.png"
    broken.write_bytes(b"this is not a PNG")
    good = _png(tmp_path / "good.png")

    # Explicit canvas: resolve_canvas returns before ever opening the file.
    with pytest.raises(CliError) as excinfo:
        load_keyframes(_spec(tmp_path, image=broken))
    assert excinfo.value.code == "image_unreadable"
    assert "--image" in excinfo.value.message

    # --end-image is never seen by resolve_canvas at all.
    with pytest.raises(CliError) as excinfo:
        load_keyframes(_spec(tmp_path, image=good, end_image=broken))
    assert excinfo.value.code == "image_unreadable"
    assert "--end-image" in excinfo.value.message


def test_the_step_count_is_read_from_the_checkpoint_not_hardcoded(tmp_path):
    """A cache can be baked for any grid, so 31 is this checkpoint's number, not the code's.

    The hardcoded constant refused `--steps 8` against a checkpoint whose cache covers exactly 8,
    quoting a number belonging to a different checkpoint.
    """
    import json
    import struct

    from h3_48gb.cli import baked_grid_points

    def fake_checkpoint(points: int) -> Path:
        root = tmp_path / f"ckpt{points}" / "transformer"
        root.mkdir(parents=True, exist_ok=True)   # called twice for the same grid below
        header = {"video_sigmas": {"dtype": "F32", "shape": [points], "data_offsets": [0, 4 * points]}}
        packed = json.dumps(header).encode()
        with open(root / "adaln_cache.safetensors", "wb") as fh:
            fh.write(struct.pack("<Q", len(packed)))
            fh.write(packed)
            fh.write(b"\x00" * 4 * points)
        return root.parent

    assert baked_grid_points(fake_checkpoint(8)) == 8
    assert baked_grid_points(fake_checkpoint(31)) == 31
    assert baked_grid_points(tmp_path / "nothing-here") is None, "a missing cache must not raise"

    spec = RunSpec(prompt="x", width=64, height=64, duration=1.0, steps=8, seed=0,
                   checkpoint=fake_checkpoint(8), outdir=tmp_path, tag="t")
    assert spec.steps == 8, "a checkpoint baked for 8 must accept --steps 8"

    with pytest.raises(CliError) as excinfo:
        RunSpec(prompt="x", width=64, height=64, duration=1.0, steps=31, seed=0,
                checkpoint=fake_checkpoint(8), outdir=tmp_path, tag="t")
    assert excinfo.value.detail["required"] == 8, "the refusal must quote this checkpoint's grid"


def test_output_does_not_default_into_the_weights_directory():
    """Clips are disposable; the 46 GB of weights beside them are not.

    They used to share `~/models`, which makes "clear out the videos" a dangerous command and
    let 1.2 GB of test output accumulate inside the model store. `H3_OUTDIR` exists so a
    permanent choice does not need a flag on every invocation.

    The env-var half runs in a subprocess rather than via `importlib.reload`: reloading rebinds
    every class in the module, so `CliError` raised afterwards is a different object than the one
    this file imported, and unrelated tests start failing in ways that have nothing to do with
    what they check. That happened.
    """
    from h3_48gb import cli

    assert "models" not in cli.DEFAULT_OUTDIR.parts, (
        f"the default output directory sits inside the model store: {cli.DEFAULT_OUTDIR}")

    result = subprocess.run(
        [sys.executable, "-c", "from h3_48gb.cli import DEFAULT_OUTDIR; print(DEFAULT_OUTDIR)"],
        capture_output=True, text=True, cwd=str(Path(__file__).resolve().parent.parent),
        env={**os.environ, "H3_OUTDIR": "/tmp/somewhere-else"})
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "/tmp/somewhere-else", (
        f"H3_OUTDIR was ignored; got {result.stdout.strip()!r}")


def test_an_alternate_adaln_cache_decides_the_step_count(tmp_path):
    """`--adaln-cache` is what makes few-step runs reachable without a symlink tree.

    Before it, running 8 steps meant building a whole fake checkpoint directory whose every
    entry symlinked to the real one except the table — easy to get subtly wrong, and it was.
    """
    import json
    import struct

    from h3_48gb.cli import _grid_points_of

    def table(points: int) -> Path:
        header = {"video_sigmas": {"dtype": "F32", "shape": [points], "data_offsets": [0, 4 * points]}}
        packed = json.dumps(header).encode()
        path = tmp_path / f"table{points}.safetensors"
        with open(path, "wb") as fh:
            fh.write(struct.pack("<Q", len(packed)))
            fh.write(packed)
            fh.write(b"\x00" * 4 * points)
        return path

    assert _grid_points_of(table(8)) == 8
    assert _grid_points_of(tmp_path / "absent.safetensors") is None, "a missing table must not raise"

    # The checkpoint's own table says 31; the alternate says 8, and the alternate must win.
    spec = spec_from_args(build_parser().parse_args(
        ["generate", "a cat", "--steps", "8", "--adaln-cache", str(table(8)),
         "--outdir", str(tmp_path)]))
    assert spec.steps == 8

    with pytest.raises(CliError) as excinfo:
        spec_from_args(build_parser().parse_args(
            ["generate", "a cat", "--steps", "31", "--adaln-cache", str(table(8)),
             "--outdir", str(tmp_path)]))
    assert excinfo.value.detail["required"] == 8, "the refusal must quote the alternate table"


# -- what a resume must not silently cross -------------------------------------------------------

def _turbo_spec(tmp_path, **overrides):
    lora = tmp_path / "lora.safetensors"
    lora.write_bytes(b"x" * 128)
    base = dict(prompt="a cat", width=64, height=64, duration=1.0, steps=31, seed=0,
                checkpoint=tmp_path, outdir=tmp_path, tag="t", turbo_lora=lora)
    base.update(overrides)
    return RunSpec(**base)


def test_the_lora_is_installed_once_even_when_resume_asks_twice(tmp_path):
    """`run_resume` installs, then calls `run_generate`, which installs again.

    Nested wrappers double the effective strength: 0.45 applies as 0.90, and every number this
    project measured for strength stops meaning anything. Nothing raises — the clip is just wrong.
    """
    from h3_48gb.cli import install_turbo_lora

    applied = []

    class FakeLoader:
        def __call__(self):
            return object()

    class FakeDit(dict):
        pass

    class FakePipe:
        def __init__(self):
            self.dit = type("P", (), {"__dict__": {"_loader": FakeLoader()}})()

    import h3_48gb.turbo as turbo
    original = turbo.apply_backbone_lora
    turbo.apply_backbone_lora = lambda dit, path, **kw: applied.append(kw.get("strength"))
    try:
        pipe = FakePipe()
        spec = _turbo_spec(tmp_path, turbo_strength=0.45)
        install_turbo_lora(pipe, spec, verbose=False)
        install_turbo_lora(pipe, spec, verbose=False)      # the resume path's second call
        pipe.dit.__dict__["_loader"]()                      # force the wrapped loader to run
    finally:
        turbo.apply_backbone_lora = original

    assert applied == [0.45], (
        f"the adapter was applied {len(applied)} times at {applied} — nesting doubles the strength")


def test_the_adapter_and_its_strength_are_part_of_the_checkpoint_identity(tmp_path):
    """Resuming at a different strength must not silently continue the interrupted run.

    Both change every latent the run produces, so a checkpoint written under one is not a
    checkpoint for the other. The AdaLN table is identified by size too, not just name: tables
    are baked now, and two bakes can share a filename while differing in step count.
    """
    from h3_48gb.pipeline import _file_identity

    small, large = tmp_path / "t.safetensors", tmp_path / "u.safetensors"
    small.write_bytes(b"x" * 10)
    large.write_bytes(b"x" * 20)
    assert _file_identity(small) != _file_identity(large), "same-name tables must not collide"
    assert _file_identity(None) is None

    class FakePipe:
        _weights_id = {}
        _adaln_cache_path = small
        _turbo_lora = small
        _turbo_strength = 0.45

        class config:
            sigma_shift_video = 12.0
            sigma_shift_audio = 3.0

        # `small` is ten junk bytes, not a table. The grid half of the identity is covered by
        # `test_the_grid_is_part_of_the_checkpoint_identity` against real safetensors.
        def _cached_sigma_grids(self):
            return None

    from h3_48gb.pipeline import LazyMiniMaxH3Pipeline

    identity = LazyMiniMaxH3Pipeline.checkpoint_identity_extra(FakePipe())
    assert identity["turbo_strength"] == 0.45
    assert identity["turbo_lora"] == [small.name, 10]
    assert identity["adaln_cache"] == [small.name, 10]

    FakePipe._turbo_strength = 0.9
    assert LazyMiniMaxH3Pipeline.checkpoint_identity_extra(FakePipe()) != identity


def test_bad_turbo_inputs_are_refused_before_any_weight_loads(tmp_path):
    """These are 28 GB and several minutes from being discovered otherwise."""
    with pytest.raises(CliError) as excinfo:
        _turbo_spec(tmp_path, turbo_lora=tmp_path / "absent.safetensors")
    assert excinfo.value.code == "lora_not_found"

    with pytest.raises(CliError) as excinfo:
        _turbo_spec(tmp_path, turbo_strength=float("nan"))
    assert excinfo.value.code == "turbo_strength_invalid"

    junk = tmp_path / "junk.safetensors"
    junk.write_bytes(b"not a safetensors file at all")
    with pytest.raises(CliError) as excinfo:
        _turbo_spec(tmp_path, adaln_cache=junk)
    assert excinfo.value.code == "adaln_cache_unreadable"


# -- the report records what produced the run --------------------------------------------------

def _run_generate_with_fakes(tmp_path, spec_fn=_turbo_spec, **overrides):
    """Run `run_generate` end to end through a faked pipeline; return `(report, spec)`.

    The brief for this test named `_run_generate_with_fakes(tmp_path, turbo_strength=0.45)`, but
    no such helper existed — this is that helper, built from the two patterns already proven
    elsewhere in this file rather than a third reimplementation: `spec_fn` (default `_turbo_spec`,
    which always sets `turbo_lora`, so a run through here always has an adapter to record — pass
    `spec_fn=_spec` for the no-adapter, no-keyframe scenario) and the `FakePipe`/`dit` shape
    `test_the_lora_is_installed_once_even_when_resume_asks_twice` uses to satisfy
    `install_turbo_lora`'s `pipe.dit.__dict__["_loader"]` access. That test's `FakePipe` is not
    callable; this one is, so `run_generate` can run its full path instead of stopping at adapter
    installation. The spec is returned alongside the report so callers can assert the report's
    values against what actually produced the run, not just that the keys exist.
    """
    class FakeLoader:
        def __call__(self):
            return object()

    class FakePipe:
        def __init__(self):
            self.dit = type("P", (), {"__dict__": {"_loader": FakeLoader()}})()

        def __call__(self, **kwargs):
            return _StubResult()

    spec = spec_fn(tmp_path, **overrides)
    report = run_generate(spec, pipeline_factory=lambda _: FakePipe(),
                          save_mp4_fn=lambda *a: None, save_wav_fn=lambda *a: None)
    return report, spec


def test_the_report_records_what_produced_the_run(tmp_path):
    """On 2026-08-10 two runs of the same prompt, canvas, duration, steps and seed measured 349
    and 265, and the cause was unrecoverable because none of this was written down.

    Checks values, not just key presence: a `key in report` check alone stays true no matter what
    a field holds, so it cannot catch a swapped `image`/`end_image`, a strength that never reached
    the report, or a `Path` slipping in where `json.dumps` needs a string.
    """
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("a cat\n")
    report, spec = _run_generate_with_fakes(tmp_path, turbo_strength=0.45,
                                            prompt_file=str(prompt_file))

    assert report["prompt"] == spec.prompt
    assert report["prompt_file"] == str(prompt_file)
    assert report["checkpoint"] == str(spec.checkpoint)
    assert isinstance(report["checkpoint"], str), (
        "a Path here passes every dict check and then breaks json.dumps hours into a real run, "
        "after the video is already rendered")
    assert report["turbo_lora"] == str(spec.turbo_lora)
    assert report["turbo_strength"] == 0.45
    # Not set on this spec: must be present as null, not merely absent from the dict. Both are
    # None here, so a swapped image/end_image would pass this line too — that distinction needs
    # two *different* images to be observable, which is what
    # test_the_report_does_not_swap_image_and_end_image below is for.
    assert report["adaln_cache"] is None
    assert report["image"] is None
    assert report["end_image"] is None
    assert report["forwards"] == report["grid_points"] - 1

    # The one line that catches a non-JSON value anywhere in the report before it ever reaches
    # disk, rather than hours into a real run when the write finally happens.
    json.dumps(report)


def test_the_report_writes_null_not_a_missing_key_when_there_was_no_lora_or_image(tmp_path):
    """`turbo_lora`/`turbo_strength`/`image`/`end_image`/`prompt_file` must let a reader tell
    "this run had none of this" apart from "the field was never recorded" -- only an explicit
    `null` can do that; a missing key cannot. `_spec` builds its `RunSpec` with a positional
    prompt (no `--prompt-file`), so `spec.prompt_file` is `None` here.
    """
    report, spec = _run_generate_with_fakes(tmp_path, spec_fn=_spec)

    assert (spec.turbo_lora is None and spec.image is None and spec.end_image is None
            and spec.prompt_file is None)
    for key in ("turbo_lora", "turbo_strength", "image", "end_image", "prompt_file"):
        assert key in report and report[key] is None, f"{key} must be present and null, not omitted"

    json.dumps(report)


def test_the_report_does_not_swap_image_and_end_image(tmp_path):
    """`report["image"] == str(spec.end_image)` (and vice versa) would pass every other test in
    this file, since the two other report tests above use no keyframes at all -- with both None,
    a swap is invisible. Only two *different* real images make it observable, so this writes two
    (the same `_png` helper `test_two_images_anchor_both_ends` and friends already use) and checks
    each report field against the specific file it names, not just against "not None".
    """
    first = _png(tmp_path / "first.png", colour=(200, 30, 30))
    last = _png(tmp_path / "last.png", colour=(30, 30, 200))

    report, spec = _run_generate_with_fakes(tmp_path, spec_fn=_spec, image=first, end_image=last)

    assert report["image"] == str(first)
    assert report["end_image"] == str(last)
    assert report["image"] != report["end_image"]

    json.dumps(report)


def test_the_lora_side_path_is_numerically_unchanged_by_its_optimizations(tmp_path):
    """`addmm` and the stack-instead-of-gather must be exact, not approximate.

    The delta used to be materialized in full before being added — 2.16 GB for `mlp.fc1` at
    37,657 rows — and the QKV path used to build a slab-ordered copy and then gather it into
    per-head order. Both are avoidable; neither may change a single value.
    """
    import mlx.core as mx

    from h3_48gb.turbo import LoRALinear, SplitQKVLoRALinear, slabs_to_interleaved

    mx.random.seed(0)
    x = mx.random.normal((3, 64))

    class Base:
        def __init__(self, out_dim):
            self.w = mx.random.normal((out_dim, 64))

        def __call__(self, v):
            return v @ self.w.T

    # plain: out + strength * (x @ A.T) @ B.T
    base = Base(96)
    a, b = mx.random.normal((8, 64)), mx.random.normal((96, 8))
    layer = LoRALinear(base, a, b, strength=0.45)
    expected = base(x) + 0.45 * ((x @ a.T) @ b.T)
    assert float(mx.abs(layer(x) - expected).max()) < 1e-4, "addmm changed the result"

    # split QKV: the stack must equal the explicit permutation
    heads, dim = 4, 8
    width = 3 * heads * dim
    qkv_base = Base(width)
    factors = {p: (mx.random.normal((8, 64)), mx.random.normal((heads * dim, 8)))
               for p in ("q", "k", "v")}
    permutation = slabs_to_interleaved(heads, dim)
    layer = SplitQKVLoRALinear(qkv_base, factors, permutation, 0.45, heads, dim)

    slabs = mx.concatenate([(x @ factors[p][0].T) @ factors[p][1].T for p in ("q", "k", "v")],
                           axis=-1)
    expected = qkv_base(x) + 0.45 * slabs[..., permutation]
    assert float(mx.abs(layer(x) - expected).max()) < 1e-4, (
        "the stack does not reproduce the slab -> per-head permutation")
