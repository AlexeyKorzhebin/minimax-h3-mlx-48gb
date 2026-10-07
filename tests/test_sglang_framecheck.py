"""spec §4.1.7, final review 2026-10-07 C1: a completed sglang scene counts as done when its mp4
decodes, holds exactly the requested 17n+5 frames, and no frame is filled with one colour. The MLX
zero-fill colour and the tile-seam detector are not used: on sglang the zero-fill colour is an
ordinary sand-grey pixel. Real ffmpeg, real mp4s."""
import gc
import json
import shutil
import subprocess
import warnings
from pathlib import Path

import numpy as np
import pytest

from h3_48gb import framecheck
from h3_48gb import queue as q
from h3_48gb.engines import sglang as sg
from _fake_sglang import FakeSglang

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")

FILL_HEX = "0x7c7468"        # framecheck.FILL_COLOR (124, 116, 104): the MLX zero-fill colour
FRAMES = 73                  # 17*4 + 5: the shortest duration sglang takes (3.04 s)


def _mp4(path: Path, *, frames: int = FRAMES, fill: tuple[int, str] | None = None,
         sand_box: bool = False) -> bytes:
    """`frames` testsrc frames at 320x240. `fill=(n, hex)` paints frame n flat in that colour;
    `sand_box` puts a 32x24 box (1 % of every frame) of the MLX zero-fill colour on every frame --
    twice the old 0.5 % floor."""
    filters = []
    if fill is not None:
        n, color = fill
        filters.append(f"drawbox=x=0:y=0:w=iw:h=ih:color={color}:t=fill:enable='eq(n,{n})'")
    if sand_box:
        filters.append(f"drawbox=x=10:y=10:w=32:h=24:color={FILL_HEX}:t=fill")
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=24",
           "-frames:v", str(frames)]
    if filters:
        cmd += ["-vf", ",".join(filters)]
    subprocess.run(cmd + ["-c:v", "libx264", "-crf", "0", "-pix_fmt", "yuv444p", str(path)],
                   check=True)
    return path.read_bytes()


def _job(tmp_path):
    root = q.layout(tmp_path / "queue")["root"]
    (tmp_path / "scenes").mkdir()
    ref = tmp_path / "ref.png"
    ref.write_bytes(b"\x89PNG\r\n\x1a\n")
    args = ["generate", "p", "--width", "896", "--height", "512", "--duration", str(FRAMES / 24),
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


def test_a_frame_filled_with_any_one_colour_is_found(tmp_path):
    _mp4(tmp_path / "sand.mp4", fill=(3, FILL_HEX))
    _mp4(tmp_path / "green.mp4", fill=(70, "0x00c000"))
    assert sg._flat_frames(tmp_path / "sand.mp4", FRAMES) == [3]
    assert sg._flat_frames(tmp_path / "green.mp4", FRAMES) == [70]


def test_one_percent_of_mlx_zero_fill_colour_is_a_clean_clip_on_sglang(tmp_path):
    """The C1 false positive: a clean clip with sand-grey content over the old 0.5 % floor."""
    path = tmp_path / "sandy.mp4"
    _mp4(path, sand_box=True)
    first = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-frames:v", "1", "-f",
                            "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True,
                           check=True).stdout
    frame = np.frombuffer(first, dtype=np.uint8).reshape(240, 320, 3)
    assert framecheck.zero_fill_fraction(frame) > framecheck.ZERO_FILL_FRACTION_THRESHOLD
    assert sg._flat_frames(path, FRAMES) == []


def test_a_frame_count_other_than_requested_is_a_failed_check(tmp_path):
    _mp4(tmp_path / "short.mp4", frames=FRAMES - 1)
    with pytest.raises(OSError) as info:
        sg._flat_frames(tmp_path / "short.mp4", FRAMES)
    assert str(info.value) == f"в mp4 {FRAMES - 1} кадров, запрошено {FRAMES}"


def test_a_flat_frame_fails_the_scene_and_keeps_the_mp4(tmp_path):
    root, job = _job(tmp_path)
    content = _mp4(tmp_path / "bad.mp4", fill=(3, FILL_HEX))
    code, log = _run(root, job, tmp_path, content)
    assert (code, log) == (1, "sglang: id=vid-1\nsglang: кадры залиты одним цветом: 3\n")
    assert Path(job.output_stem + ".mp4").read_bytes() == content
    report = json.loads(Path(job.output_stem + ".json").read_text(encoding="utf-8"))
    assert (report["status"], report["frames"]) == ("corrupt", [3])
    assert not (tmp_path / "sglang-history.jsonl").exists()


def test_a_short_mp4_fails_the_scene(tmp_path):
    root, job = _job(tmp_path)
    code, log = _run(root, job, tmp_path, _mp4(tmp_path / "short.mp4", frames=FRAMES - 17))
    assert (code, log) == (1, "sglang: id=vid-1\nsglang: mp4 не прошёл проверку кадров: "
                              f"в mp4 {FRAMES - 17} кадров, запрошено {FRAMES}\n")


def test_a_clean_clip_with_sandy_content_is_done(tmp_path):
    root, job = _job(tmp_path)
    code, log = _run(root, job, tmp_path, _mp4(tmp_path / "good.mp4", sand_box=True))
    assert (code, log) == (0, "sglang: id=vid-1\nsglang: готово, 0.0 с\n")


def test_an_undecodable_mp4_is_a_failed_scene(tmp_path):
    root, job = _job(tmp_path)
    code, log = _run(root, job, tmp_path, b"not a video")
    assert code == 1
    assert log.startswith("sglang: id=vid-1\nsglang: mp4 не прошёл проверку кадров:")


def test_the_client_closes_its_connection_after_every_call():
    """Triage of task 6: `_json` used to close the response and leave the socket to the GC."""
    fake = FakeSglang(statuses=("queued",), keep_alive=True)
    try:
        client = sg.SglangClient(fake.url)
        gc.collect()          # sockets other tests leaked must not be counted against this one
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            video_id = client.create({"prompt": "p"})["id"]
            client.get(video_id)
            gc.collect()
        ours = f"raddr=('127.0.0.1', {fake.port})"
        assert [str(w.message) for w in caught
                if w.category is ResourceWarning and ours in str(w.message)] == []
    finally:
        fake.close()
