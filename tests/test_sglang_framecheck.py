"""spec §4.1.7: a completed sglang scene passes the zero-fill check before it counts as done.
Real ffmpeg, real mp4s; the seam detector is deliberately not run (other VAE tiling)."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from h3_48gb import queue as q
from h3_48gb.engines import sglang as sg
from _fake_sglang import FakeSglang

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")

FILL_HEX = "0x7c7468"        # framecheck.FILL_COLOR (124, 116, 104)


def _mp4(path: Path, *, filled_frame: int | None) -> bytes:
    """10 testsrc frames at 320x240; frame `filled_frame` replaced by flat FILL_COLOR."""
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
           "testsrc=size=320x240:rate=10:duration=1"]
    if filled_frame is not None:
        cmd += ["-vf", f"drawbox=x=0:y=0:w=iw:h=ih:color={FILL_HEX}:t=fill:"
                       f"enable='eq(n,{filled_frame})'"]
    subprocess.run(cmd + ["-c:v", "libx264", "-crf", "0", "-pix_fmt", "yuv444p", str(path)],
                   check=True)
    return path.read_bytes()


def _job(tmp_path):
    root = q.layout(tmp_path / "queue")["root"]
    (tmp_path / "scenes").mkdir()
    ref = tmp_path / "ref.png"
    ref.write_bytes(b"\x89PNG\r\n\x1a\n")
    args = ["generate", "p", "--width", "896", "--height", "512", "--duration", str(175 / 24),
            "--tag", "t", "--outdir", str(tmp_path / "scenes"), "--ref", str(ref)]
    q.submit(root, args, "", {"output_stem": str(tmp_path / "scenes" / "h3-t-896x512")}, {})
    return root, q.claim(root)


def _run(root, job, tmp_path, content):
    fake = FakeSglang(statuses=("completed",), content=content)
    try:
        return sg.run_generate(job, root=root, outdir=tmp_path, client=sg.SglangClient(fake.url),
                               sleep=lambda s: None, clock=lambda: 1.0)
    finally:
        fake.close()


def test_the_detector_sees_the_filled_frame_and_not_a_clean_clip(tmp_path):
    bad = _mp4(tmp_path / "bad.mp4", filled_frame=3)
    good = _mp4(tmp_path / "good.mp4", filled_frame=None)
    assert sg._zero_filled_frames(tmp_path / "bad.mp4") == [3]
    assert sg._zero_filled_frames(tmp_path / "good.mp4") == []
    assert bad != good


def test_a_zero_filled_frame_fails_the_scene_and_keeps_the_mp4(tmp_path):
    root, job = _job(tmp_path)
    content = _mp4(tmp_path / "bad.mp4", filled_frame=3)
    code, log = _run(root, job, tmp_path, content)
    assert (code, log) == (1, "sglang: id=vid-1\nsglang: битые кадры (zero-fill): 3\n")
    assert Path(job.output_stem + ".mp4").read_bytes() == content
    report = json.loads(Path(job.output_stem + ".json").read_text(encoding="utf-8"))
    assert (report["status"], report["frames"]) == ("corrupt", [3])
    assert not (tmp_path / "sglang-history.jsonl").exists()


def test_a_clean_clip_is_done(tmp_path):
    root, job = _job(tmp_path)
    code, log = _run(root, job, tmp_path, _mp4(tmp_path / "good.mp4", filled_frame=None))
    assert (code, log) == (0, "sglang: id=vid-1\nsglang: готово, 0.0 с\n")


def test_an_undecodable_mp4_is_a_failed_scene(tmp_path):
    root, job = _job(tmp_path)
    code, log = _run(root, job, tmp_path, b"not a video")
    assert code == 1
    assert log.startswith("sglang: id=vid-1\nsglang: mp4 не прошёл проверку кадров:")
