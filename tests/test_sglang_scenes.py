"""Scene submission on sglang (spec §3.3.2, §3.3.5, §4.1.3): the argv `assemble` really builds,
accepted and refused by the adapter's own parser (spec §6), and the keyframe chain."""
import subprocess
from pathlib import Path

import pytest

from h3_48gb import assemble
from h3_48gb import library as lib
from h3_48gb import project as p
from h3_48gb import queue as q
from h3_48gb import web
from h3_48gb.engines import sglang_args as sa

#: Before conftest's autouse fixture replaces it with "never corrupt" for every test.
_REAL_FRAME_IS_CORRUPT = assemble._frame_is_corrupt


class _Submitted:
    def __init__(self, job_id):
        self.id = job_id


def _png(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n")
    return path


@pytest.fixture
def chain(tmp_path, monkeypatch):
    """A 5-scene video project, scenes 0-3 done, scene 4 pending, two pinned cards."""
    monkeypatch.setenv("H3_ENGINE", "sglang")
    monkeypatch.setattr(assemble.secrets, "token_hex", lambda n: "ab12")
    out = tmp_path / "outdir"
    lib.create_card(out, tag="@alice", kind="person", description="a young woman",
                    assets=[_png(out / "uploads" / "face.png")])
    lib.create_card(out, tag="@beach", kind="environment", description="a wide beach",
                    assets=[_png(out / "uploads" / "pano.png")])
    proj = p.create_project(out, "video", "Chain")
    pdir = proj.path.parent
    scenes = []
    for idx in range(5):
        clip = None
        if idx < 4:
            clip = pdir / "scenes" / f"s{idx}.mp4"
            clip.parent.mkdir(parents=True, exist_ok=True)
            clip.write_bytes(b"mp4")
        scenes.append({"idx": idx, "prompt": "@alice walks on @beach",
                       "duration": (175 if idx == 0 else 174) / 24,
                       "status": "done" if idx < 4 else "pending",
                       "job_id": f"j{idx}" if idx < 4 else None,
                       "clip_path": str(clip) if clip else None, "keyframe_path": None})
    proj.scenes = scenes
    proj.stages["scenes"] = "running"
    proj.save()
    proj.set_references([{"tag": "@alice", "version": 1}, {"tag": "@beach", "version": 1}])
    return out, proj


def _fake_run(commands):
    def run(cmd, capture_output=True, text=True):
        commands.append(list(cmd))
        if "-sseof" in cmd:
            Path(cmd[-1]).write_bytes(b"\x89PNG\r\n\x1a\n")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    return run


def test_a_fifth_chained_scene_is_submitted_with_keyframe_refs_and_one_frame_overlap(chain):
    out, proj = chain
    pdir = proj.path.parent
    submitted, commands = [], []

    def submit(root, args, note, report, estimate, kind):
        submitted.append({"args": args, "note": note, "report": report, "estimate": estimate,
                          "kind": kind})
        return _Submitted("j4")

    result = assemble.advance_project(proj, out / "queue", out, submit=submit,
                                      run=_fake_run(commands))
    keyframe = pdir / "keyframes" / "keyframe-003.png"
    assert result == {"action": "submitted_scene", "idx": 4, "job_id": "j4",
                      "keyframe": str(keyframe), "latent": None, "head_drop_frames": 1}
    alice = str(out / "library" / "alice" / "v1" / "01-face.png")
    beach = str(out / "library" / "beach" / "v1" / "01-pano.png")
    prompt = ("The video begins exactly on the provided first frame and continues it seamlessly: same characters, setting, lighting and camera style.\n\n"
              "subject_definitions:\n"
              "<Subject 1> is a young woman, appearance from <Picture 1>.\n"
              "<Subject 2> is a wide beach, appearance from <Picture 2>.\n\n"
              "<Subject 1> walks on <Subject 2>")
    assert submitted == [{
        "args": ["generate", prompt, "--width", "896", "--height", "512",
                 "--duration", str(175 / 24), "--steps", "50", "--seed", "42",
                 "--tag", "scene-4-ab12", "--outdir", str(pdir / "scenes"), "--task", "ref2va",
                 "--image", str(keyframe), "--aspect", "auto", "--ref", alice, "--ref", beach],
        "note": assemble.scene_note(proj, 4),
        "report": {"output_stem": str(pdir / "scenes" / "h3-scene-4-ab12-896x512")},
        "estimate": {"seconds": 2810.0, "source": "table", "samples": 0},
        "kind": q.KIND_GENERATE}]
    assert commands == [["ffmpeg", "-y", "-loglevel", "error", "-sseof", "-1", "-i",
                         str(pdir / "scenes" / "s3.mp4"), "-update", "1", "-q:v", "1",
                         str(keyframe)]]
    scene4 = p.load_project(proj.path).scenes[4]
    assert (scene4["status"], scene4["job_id"], scene4["head_drop_frames"],
            scene4["keyframe_path"]) == ("running", "j4", 1, str(keyframe))
    # spec §6: the adapter's own parser accepts exactly this argv
    spec = sa.parse(submitted[0]["args"])
    assert (spec.task, spec.image, spec.refs, spec.aspect_ratio, spec.frames) == \
        ("ref2va", str(keyframe), (alice, beach), "auto", 175)


def test_a_first_scene_with_a_tag_is_ref2va_with_references_and_no_keyframe(tmp_path, monkeypatch):
    monkeypatch.setattr(assemble.secrets, "token_hex", lambda n: "cd34")
    ref = str(_png(tmp_path / "r.png"))
    args, stem = assemble._scene_generate_args_sglang(
        {"idx": 0, "prompt": "@cat", "duration": 175 / 24}, keyframe=None, chained=False,
        ref2va=lib.Ref2VAScene("body", (ref,), (), ("@cat",)), track_piece=None,
        scenes_dir=tmp_path, i2v_prefix="never on a first scene")
    assert args == ["generate", "body", "--width", "896", "--height", "512",
                    "--duration", str(175 / 24), "--steps", "50", "--seed", "42",
                    "--tag", "scene-0-cd34", "--outdir", str(tmp_path), "--task", "ref2va",
                    "--ref", ref]
    assert stem == str(tmp_path / "h3-scene-0-cd34-896x512")
    assert sa.parse(args).task == "ref2va"


def test_the_parser_refuses_a_real_first_scene_argv_without_references(tmp_path, monkeypatch):
    monkeypatch.setattr(assemble.secrets, "token_hex", lambda n: "0001")
    args, _ = assemble._scene_generate_args_sglang(
        {"idx": 0, "prompt": "a cat", "duration": 175 / 24}, keyframe=None, chained=False,
        ref2va=lib.Ref2VAScene("a cat", (), (), ()), track_piece=None, scenes_dir=tmp_path)
    with pytest.raises(sa.SglangArgsError) as excinfo:
        sa.parse(args)
    assert (excinfo.value.code, excinfo.value.message) == \
        ("ref2va_needs_reference", "нужен хотя бы один референс (@тег) в сцене")


def test_i2v_prefix_is_prepended_only_to_a_chained_scene(tmp_path, monkeypatch):
    monkeypatch.setattr(assemble.secrets, "token_hex", lambda n: "ef56")
    ref = lib.Ref2VAScene("body", (str(_png(tmp_path / "r.png")),), (), ("@a",))
    chained, _ = assemble._scene_generate_args_sglang(
        {"idx": 1, "prompt": "@a", "duration": 174 / 24}, keyframe=_png(tmp_path / "k.png"),
        chained=True, ref2va=ref, track_piece=None, scenes_dir=tmp_path, i2v_prefix="Continue.")
    first, _ = assemble._scene_generate_args_sglang(
        {"idx": 0, "prompt": "@a", "duration": 175 / 24}, keyframe=None, chained=False,
        ref2va=ref, track_piece=None, scenes_dir=tmp_path, i2v_prefix="Continue.")
    assert (chained[1], first[1]) == ("Continue.\n\nbody", "body")


def test_an_unsnapped_duration_is_refused_before_submission(tmp_path):
    with pytest.raises(assemble.AssembleError):
        assemble._scene_generate_args_sglang(
            {"idx": 1, "prompt": "x", "duration": 7.0}, keyframe=_png(tmp_path / "k.png"),
            chained=True, ref2va=lib.Ref2VAScene("x", (str(_png(tmp_path / "r.png")),), (), ()),
            track_piece=None, scenes_dir=tmp_path)


def test_the_parser_refuses_a_real_chained_argv_without_references(tmp_path, monkeypatch):
    monkeypatch.setattr(assemble.secrets, "token_hex", lambda n: "0000")
    args, _ = assemble._scene_generate_args_sglang(
        {"idx": 1, "prompt": "x", "duration": 174 / 24}, keyframe=_png(tmp_path / "k.png"),
        chained=True, ref2va=lib.Ref2VAScene("x", (), (), ()), track_piece=None,
        scenes_dir=tmp_path)
    with pytest.raises(sa.SglangArgsError) as excinfo:
        sa.parse(args)
    assert (excinfo.value.code, excinfo.value.message) == \
        ("ref2va_needs_reference", "нужен хотя бы один референс (@тег) в сцене")


def test_the_parser_refuses_the_real_mlx_argv(tmp_path):
    args, _ = assemble._scene_generate_args({"idx": 0, "prompt": "x", "duration": 5.0}, None,
                                            tmp_path)
    with pytest.raises(sa.SglangArgsError) as excinfo:
        sa.parse(args)
    assert excinfo.value.message == "unsupported_on_sglang: --turbo-strength"


def test_a_corrupt_last_frame_stops_the_chain(chain, monkeypatch):
    out, proj = chain
    monkeypatch.setattr(assemble, "_frame_is_corrupt", lambda *a, **k: True)
    with pytest.raises(assemble.AssembleError):
        assemble.advance_project(proj, out / "queue", out,
                                 submit=lambda *a, **k: _Submitted("x"), run=_fake_run([]))
    assert p.load_project(proj.path).scenes[4]["status"] == "pending"


def _clip(path, *, last_color=None):
    """24 testsrc frames, 320x240, with a 32x24 box of the MLX zero-fill colour (1 % of the frame,
    twice the MLX floor) on every frame; `last_color` paints the last frame flat."""
    filters = ["drawbox=x=10:y=10:w=32:h=24:color=0x7c7468:t=fill"]
    if last_color:
        filters.append(f"drawbox=x=0:y=0:w=iw:h=ih:color={last_color}:t=fill:enable='eq(n,23)'")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc=size=320x240:rate=24", "-frames:v", "24", "-vf", ",".join(filters),
                    "-c:v", "libx264", "-crf", "0", "-pix_fmt", "yuv444p", str(path)], check=True)
    return path


@pytest.mark.skipif(subprocess.run(["which", "ffmpeg"], capture_output=True).returncode != 0,
                    reason="needs ffmpeg")
def test_the_chain_keyframe_uses_the_sglang_frame_check(tmp_path, monkeypatch):
    """Final review M1: sand-grey content over the MLX zero-fill floor chains; a flat frame not."""
    monkeypatch.setattr(assemble, "_frame_is_corrupt", _REAL_FRAME_IS_CORRUPT)
    sandy = _clip(tmp_path / "sandy.mp4")
    out = assemble._extract_last_frame(sandy, tmp_path / "kf", 0, run=subprocess.run)
    assert out == tmp_path / "kf" / "keyframe-000.png"
    flat = _clip(tmp_path / "flat.mp4", last_color="0x204060")
    with pytest.raises(assemble.AssembleError) as excinfo:
        assemble._extract_last_frame(flat, tmp_path / "kf2", 1, run=subprocess.run)
    assert str(excinfo.value) == (f"the last frame of {flat} is filled with one colour -- "
                                  "refusing to chain the next scene off it")


def test_a_chained_scene_the_worker_cannot_submit_fails_with_its_reason(chain, monkeypatch):
    """Final review C2: the worker continues the chain with nobody watching -- a failed
    submission must not leave the next scene silently `pending` with an empty queue."""
    from types import SimpleNamespace

    from h3_48gb import worker
    out, proj = chain
    scenes = proj.scenes
    scenes[3]["status"] = "running"
    proj.scenes = scenes
    proj.save()
    monkeypatch.setattr(assemble, "_frame_is_corrupt", lambda *a, **k: True)
    clip = proj.scenes[3]["clip_path"]
    job = SimpleNamespace(id="j3", note=assemble.scene_note(proj, 3),
                          output_stem=clip[:-len(".mp4")])
    worker._handle_project_scene_result(out / "queue", out, job, 0, run=_fake_run([]))
    after = p.load_project(proj.path)
    assert {k: after.scenes[4].get(k) for k in ("status", "job_id", "error")} == {
        "status": "failed", "job_id": None,
        "error": f"сцена не поставлена: AssembleError: the last frame of {clip} is filled with one "
                 f"colour -- refusing to chain the next scene off it"}
    assert (after.scenes[3]["status"], after.stages["scenes"]) == ("done", "failed")


def test_a_new_status_clears_the_scene_error(chain):
    out, proj = chain
    proj.set_scene_status(4, "failed", error="сцена не поставлена: x")
    assert p.load_project(proj.path).scenes[4]["error"] == "сцена не поставлена: x"
    proj.invalidate_scene_chain(4)
    assert "error" not in p.load_project(proj.path).scenes[4]
    proj.set_scene_status(4, "failed", error="y")
    proj.set_scene_status(4, "failed")
    assert "error" not in p.load_project(proj.path).scenes[4]


def test_the_project_summary_carries_scenes_that_failed_without_a_job(chain):
    out, proj = chain
    proj.set_scene_status(3, "failed")
    proj.set_scene_status(4, "failed", error="сцена не поставлена: x")
    assert web.project_summary(p.load_project(proj.path), [])["scene_errors"] == [
        {"idx": 4, "error": "сцена не поставлена: x"}]


def test_overlap_constant_is_the_same_in_web_and_assemble():
    assert web._SGLANG_OVERLAP_FRAMES == assemble.SGLANG_OVERLAP_FRAMES == 1


def test_two_tags_one_with_two_pictures_plus_keyframe_keep_the_exact_ref_order(chain):
    """Review of Task 4 (б)/(г): `--ref` follows `scene.images` exactly (that order *is* the
    `<Picture N>` numbering in the prompt), the keyframe is `--image` and is not numbered."""
    out, proj = chain
    pdir = proj.path.parent
    (out / "uploads" / "side.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    lib.update_card(out, "@alice", assets=[out / "uploads" / "face.png",
                                           out / "uploads" / "side.png"])
    proj.set_references([{"tag": "@beach", "version": 1}, {"tag": "@alice", "version": 2}])
    scenes = proj.scenes
    scenes[4]["prompt"] = "@alice meets @beach and @alice leaves"
    proj.scenes = scenes
    proj.save()
    submitted = []

    def submit(root, args, note, report, estimate, kind):
        submitted.append(args)
        return _Submitted("j4")

    assemble.advance_project(proj, out / "queue", out, submit=submit, run=_fake_run([]))
    keyframe = pdir / "keyframes" / "keyframe-003.png"
    a1 = str(out / "library" / "alice" / "v2" / "01-face.png")
    a2 = str(out / "library" / "alice" / "v2" / "02-side.png")
    beach = str(out / "library" / "beach" / "v1" / "01-pano.png")
    prompt = ("The video begins exactly on the provided first frame and continues it seamlessly: "
              "same characters, setting, lighting and camera style.\n\n"
              "subject_definitions:\n"
              "<Subject 1> is a young woman, appearance from <Picture 1>, <Picture 2>.\n"
              "<Subject 2> is a wide beach, appearance from <Picture 3>.\n\n"
              "<Subject 1> meets <Subject 2> and <Subject 1> leaves")
    assert submitted == [["generate", prompt, "--width", "896", "--height", "512",
                          "--duration", str(175 / 24), "--steps", "50", "--seed", "42",
                          "--tag", "scene-4-ab12", "--outdir", str(pdir / "scenes"),
                          "--task", "ref2va", "--image", str(keyframe), "--aspect", "auto",
                          "--ref", a1, "--ref", a2, "--ref", beach]]


def test_more_pictures_than_the_limit_refuse_the_submission_instead_of_dropping_any(
        chain, monkeypatch):
    """Review of Task 4 (a): every picture of the scene reaches argv and the adapter's own parser
    refuses an excess one *before* submit; the claim is rolled back, nothing is queued."""
    out, proj = chain
    monkeypatch.setenv("H3_MAX_REF_IMAGES", "1")   # the fixture's scene 4 names two cards
    submitted = []
    with pytest.raises(sa.SglangArgsError) as excinfo:
        assemble.advance_project(
            proj, out / "queue", out, submit=lambda *a, **k: submitted.append(a),
            run=_fake_run([]))
    assert (excinfo.value.code, excinfo.value.message) == \
        ("too_many_reference_images", "картинок-референсов 2, а можно не больше 1")
    assert submitted == []
    assert p.load_project(proj.path).scenes[4]["status"] == "pending"


def test_every_stage_transition_is_stamped_for_the_battle_report(tmp_path, monkeypatch):
    """Final review I7: when each stage last entered each status, in project.json."""
    proj = p.create_project(tmp_path / "out", "video", "T")
    stamps = iter(["2026-10-08T10:00:00", "2026-10-08T10:00:05", "2026-10-08T11:40:00",
                   "2026-10-08T12:00:00", "2026-10-08T12:03:00"])
    monkeypatch.setattr(p, "_now", lambda: next(stamps))
    proj.approve_stage("script")
    proj.set_stage_status("scenes", "running")
    proj.set_stage_status("scenes", "done")
    proj.set_stage_status("upscale", "running")
    proj.finish_upscale({})
    assert p.load_project(proj.path).as_dict()["stage_times"] == {
        "script": {"approved": "2026-10-08T10:00:00"},
        "scenes": {"running": "2026-10-08T10:00:05", "done": "2026-10-08T11:40:00"},
        "upscale": {"running": "2026-10-08T12:00:00", "done": "2026-10-08T12:03:00"}}


def test_the_estimate_of_a_scene_follows_its_own_steps(chain):
    out, proj = chain
    proj.scenes[4]['steps'] = 25
    proj.save()
    submitted = []

    def submit(root, args, note, report, estimate, kind):
        submitted.append(estimate)
        return _Submitted("j4")

    assemble.advance_project(p.load_project(proj.path), out / "queue", out, submit=submit,
                             run=_fake_run([]))
    assert submitted == [{"seconds": 1405.0, "source": "table", "samples": 0}]
