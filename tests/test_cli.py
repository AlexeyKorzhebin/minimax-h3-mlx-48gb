import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np

from h3_48gb.cli import (
    CliError,
    RunSpec,
    build_parser,
    main,
    run_doctor,
    run_generate,
    run_list,
    run_resume,
    spec_from_args,
)
from h3_48gb.cli import _checkpoint_path_for


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
    """Multi-hour run must not begin on a schedule that cannot finish."""
    spec = RunSpec(prompt="x", width=64, height=64, duration=1.0, steps=30, seed=0,
                   checkpoint=tmp_path, outdir=tmp_path, tag="t")
    try:
        run_generate(spec, pipeline_factory=lambda _: (lambda **kw: _StubResult()))
    except CliError as exc:
        assert "31" in str(exc), "error message must name the baked value"
        assert "AdaLN" in str(exc), "error message must explain why"
        assert exc.code == "schedule_not_baked"
    else:
        raise AssertionError("mismatched schedule must be rejected before compute")


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

def _chatty_pipeline_factory(checkpoint, verbose=True):
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
