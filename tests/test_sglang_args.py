"""The sglang argv parser (spec §3.3.2, §4.1.4-5) against hand-written argv. The argv that
`assemble` really builds is tested in test_sglang_scenes.py (task 5) -- spec §6 asks for both."""
import pytest

from h3_48gb.engines import sglang_args as sa
from h3_48gb.engines.sglang_args import SglangArgsError, SglangSpec


def _png(path):
    path.write_bytes(b"\x89PNG\r\n\x1a\n")
    return str(path)


def _argv(tmp_path, *extra, duration="7.291666666666667"):
    return ["generate", "кот на подоконнике", "--width", "896", "--height", "512",
            "--duration", duration, "--tag", "scene-0-ab12", "--outdir", str(tmp_path), *extra]


def test_the_frame_grid_is_17n_plus_5():
    assert [sa.grid_frames_up(n) for n in (1, 5, 6, 120, 124, 168, 175, 192, 209)] == \
        [5, 5, 22, 124, 124, 175, 175, 192, 209]


def test_a_first_scene_with_one_reference_parses_to_ref2va_with_table_aspect(tmp_path):
    ref = _png(tmp_path / "a.png")
    spec = sa.parse(_argv(tmp_path, "--ref", ref))
    assert spec == SglangSpec(prompt="кот на подоконнике", width=896, height=512,
                              duration=7.291666666666667, frames=175, steps=50, seed=42,
                              tag="scene-0-ab12", outdir=str(tmp_path), image=None, refs=(ref,),
                              audio=(), task="ref2va", short_edge=512, aspect_ratio="16:9")


def test_a_scene_without_any_reference_is_refused(tmp_path):
    """spec §4.1.3: the ref2va server serves only ref2va; a scene with nothing to reference has
    no task it can run as -- refused here, not by a render that fails an hour later."""
    with pytest.raises(SglangArgsError) as excinfo:
        sa.parse(_argv(tmp_path))
    assert (excinfo.value.code, excinfo.value.message) == \
        ("ref2va_needs_reference", "нужен хотя бы один референс (@тег) в сцене")


def test_a_chained_scene_with_refs_and_audio_parses_to_ref2va(tmp_path):
    kf, r1, r2 = _png(tmp_path / "kf.png"), _png(tmp_path / "a.png"), _png(tmp_path / "b.png")
    wav = tmp_path / "piece.wav"
    wav.write_bytes(b"RIFF")
    spec = sa.parse(_argv(tmp_path, "--image", kf, "--ref", r1, "--ref", r2, "--audio", str(wav),
                          "--aspect", "auto", "--steps", "50", "--seed", "7", "--task", "ref2va"))
    assert (spec.task, spec.image, spec.refs, spec.audio, spec.aspect_ratio, spec.seed) == \
        ("ref2va", kf, (r1, r2), (str(wav),), "auto", 7)


@pytest.mark.parametrize("flag", ["--turbo-strength", "--checkpoint", "--latent", "--save-latent-tail",
                                  "--adaln-cache", "--end-image", "--prompt-file"])
def test_any_flag_outside_the_list_is_refused_by_name(tmp_path, flag):
    with pytest.raises(SglangArgsError) as excinfo:
        sa.parse(_argv(tmp_path, flag, "1"))
    assert (excinfo.value.code, excinfo.value.message) == \
        ("unsupported_on_sglang", f"unsupported_on_sglang: {flag}")


@pytest.mark.parametrize("width,height", [(1344, 768), (896, 576), (512, 512)])
def test_a_canvas_outside_the_table_is_refused(tmp_path, width, height):
    argv = _argv(tmp_path)
    argv[argv.index("--width") + 1] = str(width)
    argv[argv.index("--height") + 1] = str(height)
    with pytest.raises(SglangArgsError) as excinfo:
        sa.parse(argv)
    assert excinfo.value.code == "canvas_unsupported_on_sglang"


@pytest.mark.parametrize("duration", ["2.5", "15.5"])
def test_duration_outside_3_to_15_is_refused(tmp_path, duration):
    with pytest.raises(SglangArgsError) as excinfo:
        sa.parse(_argv(tmp_path, duration=duration))
    assert excinfo.value.code == "duration_out_of_range"


def test_an_off_grid_duration_names_the_next_grid_point(tmp_path):
    with pytest.raises(SglangArgsError) as excinfo:
        sa.parse(_argv(tmp_path, duration="7.0"))
    assert (excinfo.value.code, excinfo.value.detail) == \
        ("duration_off_grid", {"frames": 168, "next_grid_frames": 175,
                               "next_grid_seconds": 175 / 24})


@pytest.mark.parametrize("task", ["t2va", "fl2va"])
def test_any_task_but_ref2va_is_refused(tmp_path, task):
    with pytest.raises(SglangArgsError) as excinfo:
        sa.parse(_argv(tmp_path, "--task", task, "--ref", _png(tmp_path / "a.png")))
    assert excinfo.value.code == "sglang_args_invalid"


def test_a_keyframe_without_any_reference_is_refused(tmp_path):
    with pytest.raises(SglangArgsError) as excinfo:
        sa.parse(_argv(tmp_path, "--image", _png(tmp_path / "kf.png")))
    assert (excinfo.value.code, excinfo.value.message) == \
        ("ref2va_needs_reference", "нужен хотя бы один референс (@тег) в сцене")


def test_an_audio_reference_alone_is_enough(tmp_path):
    piece = tmp_path / "piece.wav"
    piece.write_bytes(b"RIFF")
    assert sa.parse(_argv(tmp_path, "--audio", str(piece))).task == "ref2va"


def test_more_refs_than_the_limit_is_refused(tmp_path):
    refs = []
    for i in range(3):
        refs += ["--ref", _png(tmp_path / f"r{i}.png")]
    with pytest.raises(SglangArgsError) as excinfo:
        sa.parse(_argv(tmp_path, *refs), environ={"H3_MAX_REF_IMAGES": "2"})
    assert (excinfo.value.code, excinfo.value.detail) == \
        ("too_many_reference_images", {"count": 3, "limit": 2})


def test_the_default_ref_limit_is_six():
    assert sa.max_ref_images({}) == 6


def test_a_missing_condition_file_is_refused_unless_files_are_not_checked(tmp_path):
    argv = _argv(tmp_path, "--ref", str(tmp_path / "nope.png"))
    with pytest.raises(SglangArgsError) as excinfo:
        sa.parse(argv)
    assert excinfo.value.code == "condition_file_missing"
    assert sa.parse(argv, check_files=False).refs == (str(tmp_path / "nope.png"),)


def test_a_repeated_single_value_flag_is_refused(tmp_path):
    with pytest.raises(SglangArgsError) as excinfo:
        sa.parse(_argv(tmp_path, "--seed", "1", "--seed", "2"))
    assert excinfo.value.code == "sglang_args_invalid"


def test_aspect_must_be_auto_or_the_table_aspect(tmp_path):
    with pytest.raises(SglangArgsError) as excinfo:
        sa.parse(_argv(tmp_path, "--aspect", "9:16", "--ref", _png(tmp_path / "a.png")))
    assert excinfo.value.code == "sglang_args_invalid"


def test_dry_run_report_and_output_stem(tmp_path):
    spec = sa.parse(_argv(tmp_path, "--ref", _png(tmp_path / "a.png")))
    assert sa.output_stem(spec) == f"{tmp_path}/h3-scene-0-ab12-896x512"
    assert sa.dry_run_report(spec) == {
        "dry_run": True, "engine": "sglang", "output_stem": f"{tmp_path}/h3-scene-0-ab12-896x512",
        "canvas": "896x512", "duration_seconds": 7.291666666666667, "frames": 175,
        "grid_points": 50, "task": "ref2va"}


def test_the_duration_is_normalised_to_the_grid_frames(tmp_path):
    spec = sa.parse(_argv(tmp_path, "--ref", _png(tmp_path / "a.png"), duration="7.31"))
    assert (spec.frames, spec.duration) == (175, 175 / 24)
    assert sa.grid_frames_up(spec.frames) == spec.frames
