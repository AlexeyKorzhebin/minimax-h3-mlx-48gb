"""LTX upscale (spec §4.2) against a fake ComfyUI, with real ffmpeg on tiny lavfi clips."""
import json
import subprocess
from pathlib import Path

import numpy as np
import pytest

from h3_48gb import project as p
from h3_48gb.engines import ltx
from h3_48gb.engines import motion
from _fake_comfy import FakeComfy

TEMPLATE = json.loads((Path(ltx.__file__).with_name("ltx_workflow.json")).read_text())


TESTSRC = "testsrc=size=64x64:rate=24:duration=1"
STILL = "color=c=gray:size=64x64:rate=24:duration=1"
BUSY = "color=c=gray:size=64x64:rate=24:duration=1,noise=alls=100:allf=t"


def _clip(path: Path, video: str = TESTSRC) -> Path:
    """A 1 s, 24-frame clip with an audio track (run_ltx.sh muxes the part's own audio back)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", video,
                    "-f", "lavfi", "-i", "sine=frequency=220:duration=1", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)], check=True)
    return path


def _frames(path) -> int:
    out = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v",
                          "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True)
    return int(out.stdout)


def _has_audio(path) -> bool:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
                          "stream=index", "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    return bool(out.stdout.strip())


def test_pad_frames_is_8k_plus_1_like_run_ltx_sh():
    assert [ltx.pad_frames(n) for n in (24, 124, 175, 209)] == [25, 129, 177, 209]


def test_build_workflow_changes_exactly_five_inputs():
    wf = ltx.build_workflow(input_name="s-a1-pad.mp4", prompt="a beach", strength=0.3,
                            prefix="h3panel/proj/s-a1")
    expected = json.loads(json.dumps(TEMPLATE))
    expected["10"]["inputs"]["file"] = "s-a1-pad.mp4"
    expected["20"]["inputs"]["text"] = "a beach"
    expected["2"]["inputs"]["strength_model"] = 0.3
    expected["33"]["inputs"]["noise_seed"] = 42
    expected["42"]["inputs"]["filename_prefix"] = "h3panel/proj/s-a1/f"
    assert wf == expected
    assert (wf["32"]["inputs"]["denoise"], wf["32"]["inputs"]["steps"]) == (0.1, 4)


def test_template_graph_is_well_formed_and_matches_the_source_inputs():
    """The validate_workflow.py rules that need no ComfyUI: every link points at a node that
    exists, and every node's input keys are exactly build_ltx_workflow.py's (an unknown input
    passes ComfyUI's own validate_prompt and only fails in execute())."""
    expected_inputs = {
        "1": {"unet_name", "weight_dtype"}, "2": {"model", "lora_name", "strength_model"},
        "3": {"clip_name", "type", "device"}, "4": {"vae_name"}, "5": {"vae_name"},
        "6": {"model_name"}, "10": {"file"}, "11": {"video"}, "12": {"pixels", "vae"},
        "13": {"samples", "upscale_model", "vae"}, "14": {"image", "batch_index", "length"},
        "15": {"vae", "image", "latent", "strength", "bypass"}, "16": {"audio", "audio_vae"},
        "17": {"video_latent", "audio_latent"}, "20": {"text", "clip"}, "21": {"text", "clip"},
        "22": {"positive", "negative", "frame_rate"},
        "30": {"model", "positive", "negative", "video_cfg", "audio_cfg"},
        "31": {"sampler_name"}, "32": {"model", "scheduler", "steps", "denoise"},
        "33": {"noise_seed"}, "34": {"noise", "guider", "sampler", "sigmas", "latent_image"},
        "40": {"av_latent"},
        "41": {"samples", "vae", "tile_size", "overlap", "temporal_size", "temporal_overlap"},
        "42": {"images", "filename_prefix"}}
    assert {nid: set(node["inputs"]) for nid, node in TEMPLATE.items()} == expected_inputs
    for node in TEMPLATE.values():
        for value in node["inputs"].values():
            if isinstance(value, list) and len(value) == 2 and isinstance(value[1], int):
                assert value[0] in TEMPLATE


def test_motion_is_measured_over_all_parts_together():
    px = 32 * 24
    still = bytes(3 * px)
    moving = (bytes(px) + bytes([8]) * px + bytes(px))
    answers = {"a.mp4": still, "b.mp4": moving}

    def run(cmd, capture_output=True):
        return subprocess.CompletedProcess(cmd, 0, answers[Path(cmd[cmd.index("-i") + 1]).name], b"")

    assert motion.clip_motion([Path("a.mp4"), Path("b.mp4")], run=run) == 4.0
    assert (motion.clip_motion([Path("a.mp4")], run=run),
            motion.clip_motion([Path("b.mp4")], run=run)) == (0.0, 8.0)
    assert [motion.lora_for(m) for m in (0.0, 2.99, 3.0, 7.49, 7.5, 8.0)] == \
        [0.6, 0.6, 0.3, 0.3, 0.15, 0.15]


def test_upscale_part_pads_uploads_waits_and_muxes(tmp_path):
    clip = _clip(tmp_path / "scenes" / "h3-s0-896x512.mp4")
    out_dir = tmp_path / "comfy-out"
    fake = FakeComfy(out_dir, frames=(25,))
    try:
        result = ltx.upscale_part(clip, prompt="a beach", strength=0.3,
                                  prefix="h3panel/proj/h3-s0-896x512-a1",
                                  client=ltx.ComfyClient(fake.url), comfy_output=out_dir,
                                  run=subprocess.run, sleep=lambda s: None)
    finally:
        fake.close()
    assert result == clip.with_name("h3-s0-896x512-ltx.mp4")
    assert (_frames(result), _has_audio(result)) == (24, True)
    assert [name for name, _ in fake.uploads] == ["h3-s0-896x512-a1-pad.mp4"]
    assert fake.prompts == [ltx.build_workflow(input_name="h3-s0-896x512-a1-pad.mp4",
                                               prompt="a beach", strength=0.3,
                                               prefix="h3panel/proj/h3-s0-896x512-a1")]
    assert fake.history_calls == ["p1", "p1"]
    assert not (out_dir / "h3panel" / "proj" / "h3-s0-896x512-a1").exists()   # frames removed
    assert not (clip.parent / "ltx-work" / "h3-s0-896x512-a1-pad.mp4").exists()


def test_upscale_prefix_is_unique_per_attempt_and_frame_count_is_checked(tmp_path):
    """Review Focus 3: frames left by an earlier attempt are never read; a wrong count fails."""
    clip = _clip(tmp_path / "scenes" / "s.mp4")
    out_dir = tmp_path / "comfy-out"
    stale = out_dir / "h3panel" / "proj" / "s-a1"
    stale.mkdir(parents=True)
    for i in range(1, 40):
        (stale / f"f_{i:05d}_.png").write_bytes(b"old")
    fake = FakeComfy(out_dir, frames=(24,))
    try:
        with pytest.raises(ltx.UpscaleError) as excinfo:
            ltx.upscale_part(clip, prompt="x", strength=0.3, prefix="h3panel/proj/s-a2",
                             client=ltx.ComfyClient(fake.url), comfy_output=out_dir,
                             run=subprocess.run, sleep=lambda s: None)
    finally:
        fake.close()
    assert str(excinfo.value) == "ComfyUI вернул 24 кадров вместо 25 (h3panel/proj/s-a2)"
    assert not clip.with_name("s-ltx.mp4").exists()


def test_a_comfy_execution_error_fails_the_part(tmp_path):
    clip = _clip(tmp_path / "s.mp4")
    fake = FakeComfy(tmp_path / "o", fail=True)
    try:
        with pytest.raises(ltx.UpscaleError) as excinfo:
            ltx.upscale_part(clip, prompt="x", strength=0.3, prefix="h3panel/p/s-a1",
                             client=ltx.ComfyClient(fake.url), comfy_output=tmp_path / "o",
                             run=subprocess.run, sleep=lambda s: None)
    finally:
        fake.close()
    assert str(excinfo.value) == "ComfyUI: OOM"


def test_one_strength_for_the_whole_clip(tmp_path):
    out = tmp_path / "out"
    proj = p.create_project(out, "video", "Up")
    pdir = proj.path.parent
    still = _clip(pdir / "scenes" / "still.mp4", STILL)
    busy = _clip(pdir / "scenes" / "busy.mp4", BUSY)
    per_part = [motion.lora_for(motion.clip_motion([c], run=subprocess.run)) for c in (still, busy)]
    assert per_part[0] != per_part[1], "fixture must make a per-scene choice differ"
    proj.scenes = [{"idx": i, "prompt": f"scene {i}", "duration": 1.0, "status": "done",
                    "job_id": f"j{i}", "clip_path": str(c), "keyframe_path": None}
                   for i, c in enumerate((still, busy))]
    proj.save()
    fake = FakeComfy(tmp_path / "comfy-out", frames=(25,))
    try:
        code, log = ltx.run_upscale(proj.path, client=ltx.ComfyClient(fake.url),
                                    comfy_output=tmp_path / "comfy-out", run=subprocess.run,
                                    attempt="up1", sleep=lambda s: None)
    finally:
        fake.close()
    assert code == 0, log
    strengths = [wf["2"]["inputs"]["strength_model"] for wf in fake.prompts]
    combined = motion.lora_for(motion.clip_motion([still, busy], run=subprocess.run))
    assert strengths == [combined, combined]
    assert [wf["20"]["inputs"]["text"] for wf in fake.prompts] == ["scene 0", "scene 1"]
    assert [wf["42"]["inputs"]["filename_prefix"] for wf in fake.prompts] == \
        [f"h3panel/{proj.id}/still-up1/f", f"h3panel/{proj.id}/busy-up1/f"]
    reloaded = p.load_project(proj.path)
    assert [s["ltx_path"] for s in reloaded.scenes] == \
        [str(still.with_name("still-ltx.mp4")), str(busy.with_name("busy-ltx.mp4"))]
    assert reloaded.stages["upscale"] == "done"


def test_multipart_upload_body_is_exact(tmp_path):
    clip = tmp_path / "a.mp4"
    clip.write_bytes(b"VIDEO")
    fake = FakeComfy(tmp_path)
    try:
        ltx.ComfyClient(fake.url, boundary="BOUNDARY").upload(clip, "a-pad.mp4")
    finally:
        fake.close()
    assert fake.uploads == [("a-pad.mp4", (
        b"--BOUNDARY\r\nContent-Disposition: form-data; name=\"image\"; filename=\"a-pad.mp4\"\r\n"
        b"Content-Type: video/mp4\r\n\r\nVIDEO\r\n"
        b"--BOUNDARY\r\nContent-Disposition: form-data; name=\"type\"\r\n\r\ninput\r\n"
        b"--BOUNDARY\r\nContent-Disposition: form-data; name=\"overwrite\"\r\n\r\ntrue\r\n"
        b"--BOUNDARY--\r\n"))]


def test_project_stage_upscale_exists_and_old_projects_migrate_to_draft(tmp_path):
    proj = p.create_project(tmp_path, "video", "T")
    assert proj.stages["upscale"] == "draft"
    data = json.loads(proj.path.read_text())
    del data["stages"]["upscale"]
    proj.path.write_text(json.dumps(data))
    assert p.load_project(proj.path).stages["upscale"] == "draft"


def test_upscale_job_args_are_shape_checked(tmp_path):
    from h3_48gb import queue as q
    root = q.layout(tmp_path / "queue")["root"]
    with pytest.raises(q.QueueError):
        q.submit(root, ["upscale"], "", {"output_stem": str(tmp_path / "u")}, {}, kind=q.KIND_UPSCALE)
    with pytest.raises(q.QueueError):
        q.submit(root, ["upscale", "--project", "x"], "", {"output_stem": str(tmp_path / "u")}, {})


def test_the_upscale_job_asks_the_dispatcher_for_ltx(tmp_path, monkeypatch):
    from h3_48gb import queue as q
    from h3_48gb import worker
    from _fake_dispatcher import FakeDispatcher

    monkeypatch.setenv("H3_ENGINE", "sglang")
    disp = FakeDispatcher(acquire=({"ok": True, "state": "ready", "engine": "ltx"},))
    out = tmp_path / "out"
    proj = p.create_project(out, "video", "Up")
    clip = _clip(proj.path.parent / "scenes" / "s.mp4")
    proj.scenes = [{"idx": 0, "prompt": "x", "duration": 1.0, "status": "done", "job_id": "j",
                    "clip_path": str(clip), "keyframe_path": None}]
    proj.save()
    comfy = FakeComfy(tmp_path / "co", frames=(25,))
    monkeypatch.setenv("H3_DISPATCHER_URL", disp.url)
    monkeypatch.setenv("H3_COMFY_URL", comfy.url)
    monkeypatch.setenv("H3_COMFY_OUTPUT_DIR", str(tmp_path / "co"))
    root = q.layout(out / "queue")["root"]
    q.submit(root, ["upscale", "--project", str(proj.path)], "", {"output_stem": str(out / "u")},
             {}, kind=q.KIND_UPSCALE)
    job_id = q.scan(root)[0][0].id
    try:
        code = worker.run_job(root, q.claim(root), outdir=out)
    finally:
        disp.close()
        comfy.close()
    assert code == 0
    assert [c[2] for c in disp.calls if c[1] == "/acquire"] == [{"engine": "ltx"}]
    assert p.load_project(proj.path).scenes[0]["ltx_path"] == str(clip.with_name("s-ltx.mp4"))
    (prefix,) = [wf["42"]["inputs"]["filename_prefix"] for wf in comfy.prompts]
    import re
    assert re.fullmatch(rf"h3panel/{proj.id}/s-{re.escape(job_id)}-\d+/f", prefix), prefix


def test_project_active_job_sees_a_pending_upscale_job(tmp_path):
    from h3_48gb import queue as q
    from h3_48gb import web
    proj = p.create_project(tmp_path / "out", "video", "Up")
    root = q.layout(tmp_path / "queue")["root"]
    q.submit(root, ["upscale", "--project", str(proj.path)], "", {"output_stem": str(tmp_path / "u")},
             {}, kind=q.KIND_UPSCALE)
    jobs = q.scan(root)[0]
    active = web._project_active_job(proj, jobs)
    assert (active["kind"], active["job"]["kind"], active["job"]["args"]) == \
        ("upscale", "upscale", ["upscale", "--project", str(proj.path)])
