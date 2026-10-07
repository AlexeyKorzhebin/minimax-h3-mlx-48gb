"""The sglang adapter against a fake sglang (spec §4.1.3, §4.1.6-7, §5). Every payload is an
exact dict, one per case of §4.1.3."""
import pytest

from h3_48gb import assemble
from h3_48gb import library as lib
from h3_48gb import queue as q
from h3_48gb.engines import sglang as sg
from h3_48gb.engines import sglang_args as sa
from _fake_sglang import FakeSglang

@pytest.fixture(autouse=True)
def _no_real_frame_decode(monkeypatch):
    """The fake serves placeholder bytes, not a decodable mp4; the real zero-fill check is
    exercised in test_sglang_framecheck.py."""
    monkeypatch.setattr(sg, "_flat_frames", lambda mp4, expected: [])


COMMON = {"model": "MiniMaxAI/MiniMax-H3", "num_outputs_per_prompt": 1,
          "num_inference_steps": 50, "flow_shift": 12.0, "audio_flow_shift": 3.0,
          "seed": 42, "quality": "lossless"}


def _png(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n")
    return str(path)


def _spec(tmp_path, *extra, duration=175 / 24, prompt="p"):
    return sa.parse(["generate", prompt, "--width", "896", "--height", "512",
                     "--duration", str(duration), "--tag", "t", "--outdir", str(tmp_path),
                     *extra])


def test_payload_first_scene_with_references(tmp_path):
    a, b = _png(tmp_path / "a.png"), _png(tmp_path / "b.png")
    assert sg.build_payload(_spec(tmp_path, "--ref", a, "--ref", b)) == {
        **COMMON, "prompt": "p", "task": "ref2va",
        "conditions": [{"type": "image", "uri": a, "role": "reference"},
                       {"type": "image", "uri": b, "role": "reference"}],
        "target": {"short_edge": 512, "aspect_ratio": "16:9", "duration_seconds": 175 / 24}}


def test_payload_chained_scene_keeps_references_and_asks_auto(tmp_path):
    kf, a = _png(tmp_path / "kf.png"), _png(tmp_path / "a.png")
    assert sg.build_payload(_spec(tmp_path, "--image", kf, "--aspect", "auto", "--ref", a)) == {
        **COMMON, "prompt": "p", "task": "ref2va",
        "conditions": [{"type": "image", "uri": kf, "role": "keyframe", "frame_index": 0},
                       {"type": "image", "uri": a, "role": "reference"}],
        "target": {"short_edge": 512, "aspect_ratio": "auto", "duration_seconds": 175 / 24}}


def test_payload_clip_scene_adds_its_track_piece_as_audio_reference(tmp_path):
    kf = _png(tmp_path / "kf.png")
    piece = tmp_path / "piece.wav"
    piece.write_bytes(b"RIFF")
    assert sg.build_payload(_spec(tmp_path, "--image", kf, "--aspect", "auto",
                                  "--audio", str(piece))) == {
        **COMMON, "prompt": "p", "task": "ref2va",
        "conditions": [{"type": "image", "uri": kf, "role": "keyframe", "frame_index": 0},
                       {"type": "audio", "uri": f"file://{piece}", "role": "reference"}],
        "target": {"short_edge": 512, "aspect_ratio": "auto", "duration_seconds": 175 / 24}}


def test_two_tags_one_with_two_pictures_plus_keyframe_end_to_end(tmp_path, monkeypatch):
    """spec §6: the library fixture through assemble's real argv to the exact payload; a mutation
    of the picture order must fail here."""
    monkeypatch.setattr(assemble.secrets, "token_hex", lambda n: "ab12")
    out = tmp_path / "out"
    lib.create_card(out, tag="@alice", kind="person", description="a young woman",
                    assets=[_png(out / "u" / "face.png"), _png(out / "u" / "back.png")])
    lib.create_card(out, tag="@beach", kind="environment", description="a wide beach",
                    assets=[_png(out / "u" / "pano.png")])
    refs = [{"tag": "@alice", "version": 1}, {"tag": "@beach", "version": 1}]
    ref2va = lib.build_ref2va("@alice walks on @beach", refs, out)
    kf = _png(tmp_path / "kf.png")
    args, _ = assemble._scene_generate_args_sglang(
        {"idx": 1, "prompt": "@alice walks on @beach", "duration": 174 / 24}, keyframe=kf,
        chained=True, ref2va=ref2va, track_piece=None, scenes_dir=tmp_path)
    L = out / "library"
    assert sg.build_payload(sa.parse(args)) == {
        **COMMON,
        "prompt": ("subject_definitions:\n"
                   "<Subject 1> is a young woman, appearance from <Picture 1>, <Picture 2>.\n"
                   "<Subject 2> is a wide beach, appearance from <Picture 3>.\n\n"
                   "<Subject 1> walks on <Subject 2>"),
        "task": "ref2va",
        "conditions": [
            {"type": "image", "uri": kf, "role": "keyframe", "frame_index": 0},
            {"type": "image", "uri": str(L / "alice" / "v1" / "01-face.png"), "role": "reference"},
            {"type": "image", "uri": str(L / "alice" / "v1" / "02-back.png"), "role": "reference"},
            {"type": "image", "uri": str(L / "beach" / "v1" / "01-pano.png"), "role": "reference"}],
        "target": {"short_edge": 512, "aspect_ratio": "auto", "duration_seconds": 175 / 24}}


def test_normalize_rewrites_only_known_legacy_roots():
    assert sg.normalize_h3_uri("/home/alex/h3-bench/inputs/a.png") == \
        "/home/alex/Projects/h3-bench/inputs/a.png"
    assert sg.normalize_h3_uri("file:///home/alex/h3-bench/outputs/x.wav") == \
        "file:///home/alex/Outputs/h3-bench/x.wav"
    assert sg.normalize_h3_uri("/home/alex/Outputs/h3-panel/p/k.png") == \
        "/home/alex/Outputs/h3-panel/p/k.png"


# -- run_generate ------------------------------------------------------------------------------


@pytest.fixture
def queued(tmp_path):
    root = q.layout(tmp_path / "queue")["root"]
    (tmp_path / "scenes").mkdir()
    ref = tmp_path / "ref.png"
    ref.write_bytes(b"\x89PNG\r\n\x1a\n")
    args = ["generate", "p", "--width", "896", "--height", "512", "--duration", str(175 / 24),
            "--tag", "t", "--outdir", str(tmp_path / "scenes"), "--ref", str(ref)]
    job = q.submit(root, args, "", {"output_stem": str(tmp_path / "scenes" / "h3-t-896x512")}, {})
    return root, q.claim(root)


class _Clock:
    def __init__(self, *values):
        self.values = list(values)

    def __call__(self):
        return self.values.pop(0) if len(self.values) > 1 else self.values[0]


def _run(job, root, tmp_path, fake, **kw):
    kw.setdefault("sleep", lambda s: None)
    kw.setdefault("clock", _Clock(0.0, 100.0))
    return sg.run_generate(job, root=root, outdir=tmp_path, client=sg.SglangClient(fake.url), **kw)


def _report(job):
    import json
    from pathlib import Path
    return json.loads(Path(job.output_stem + ".json").read_text(encoding="utf-8"))


def test_completed_downloads_and_reports(queued, tmp_path):
    root, job = queued
    fake = FakeSglang(statuses=("queued", "completed"))
    try:
        # POST at 10, `completed` seen at 80, downloaded at 95, frames checked at 110
        code, log = _run(job, root, tmp_path, fake, clock=_Clock(10.0, 80.0, 95.0, 110.0))
    finally:
        fake.close()
    from pathlib import Path
    assert (code, log) == (0, "sglang: id=vid-1\nsglang: готово, 100.0 с\n")
    assert Path(job.output_stem + ".mp4").read_bytes() == b"MP4-BYTES"
    assert not Path(job.output_stem + ".mp4.part").exists()
    payload = sg.build_payload(sa.parse(job.args, check_files=False))
    assert fake.posts == [payload]
    assert fake.gets == ["vid-1", "vid-1"]
    assert _report(job) == {"engine": "sglang", "status": "completed", "id": "vid-1",
                            "wall_s": 100.0, "inference_time_s": 300.5,
                            "peak_memory_mb": 47000.0, "post_at": 10.0, "completed_at": 80.0,
                            "server_s": 70.0, "download_s": 15.0, "framecheck_s": 15.0,
                            "payload": payload}
    assert [j for j in q.scan(root)[0] if j.id == job.id][0].engine_ref == "vid-1"
    import json
    history = (tmp_path / "sglang-history.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line) for line in history] == \
        [{"width": 896, "height": 512, "frames": 175, "steps": 50, "wall_s": 100.0}]


def test_a_4xx_fails_at_once_and_keeps_the_body(queued, tmp_path):
    root, job = queued
    detail = "target.duration_seconds must be in [3, 15], got 2"
    fake = FakeSglang(post_status=400, post_body={"detail": detail})
    try:
        code, log = _run(job, root, tmp_path, fake)
    finally:
        fake.close()
    assert (code, log) == (1, f"sglang отказал (400): {detail}\n")
    assert fake.gets == []
    report = _report(job)
    assert (report["status"], report["http_status"], report["detail"], report["id"]) == \
        ("rejected", 400, detail, None)


def test_sglang_failed_is_failed_with_its_message(queued, tmp_path):
    root, job = queued
    fake = FakeSglang(statuses=("failed",), error={"message": "CUDA out of memory"})
    try:
        code, log = _run(job, root, tmp_path, fake)
    finally:
        fake.close()
    assert (code, log) == (1, "sglang: id=vid-1\nsglang: сцена упала: CUDA out of memory\n")


def test_one_poll_interval_is_twenty_one_second_slices(queued, tmp_path):
    root, job = queued
    fake = FakeSglang(statuses=("queued", "completed"))
    sleeps = []
    try:
        _run(job, root, tmp_path, fake, sleep=sleeps.append)
    finally:
        fake.close()
    assert sleeps == [1.0] * 20


def test_one_lost_poll_waits_thirty_one_second_slices(queued, tmp_path):
    root, job = queued
    fake = FakeSglang(statuses=("completed",), drop_gets=1)
    sleeps = []
    try:
        _run(job, root, tmp_path, fake, sleep=sleeps.append)
    finally:
        fake.close()
    assert sleeps == [1.0] * 30


def test_wall_time_after_a_resume_counts_from_the_original_post(queued, tmp_path):
    root, job = queued
    fake = FakeSglang(statuses=("completed",))
    fake.ids.append("vid-7")                       # posted by the previous worker
    q.set_running_fields(root, job.id, engine_ref="vid-7", engine_submitted_at=50.0)
    resumed = [j for j in q.scan(root)[0] if j.id == job.id][0]
    try:
        code, log = _run(resumed, root, tmp_path, fake, clock=_Clock(130.0))
    finally:
        fake.close()
    assert (code, log) == (0, "sglang: продолжаю опрос id=vid-7 после рестарта\n"
                              "sglang: готово, 80.0 с\n")
    assert (fake.posts, _report(resumed)["wall_s"]) == ([], 80.0)


def test_two_dropped_polls_are_survived(queued, tmp_path):
    root, job = queued
    fake = FakeSglang(statuses=("completed",), drop_gets=2)
    sleeps = []
    try:
        code, _ = _run(job, root, tmp_path, fake, sleep=sleeps.append)
    finally:
        fake.close()
    assert code == 0
    assert fake.gets == ["vid-1", "vid-1", "vid-1"]


def test_five_dropped_polls_mean_h3_is_gone(queued, tmp_path):
    root, job = queued
    fake = FakeSglang(statuses=("queued",), drop_gets=99)
    try:
        code, log = _run(job, root, tmp_path, fake)
    finally:
        fake.close()
    assert (code, log) == (1, "sglang: id=vid-1\nsglang: H3 пропал — 5 попыток через 30 с без ответа\n")
    assert len(fake.gets) == 5


def test_download_is_atomic(queued, tmp_path):
    """Review Focus 4: a truncated /content must never leave a `<stem>.mp4` that queue.reconcile
    would take for a finished run."""
    from pathlib import Path
    root, job = queued
    fake = FakeSglang(statuses=("completed",), truncate_contents=99)
    try:
        code, log = _run(job, root, tmp_path, fake)
    finally:
        fake.close()
    assert code == 1
    assert not Path(job.output_stem + ".mp4").exists()
    assert not Path(job.output_stem + ".mp4.part").exists()


def test_a_truncated_download_is_retried_and_then_succeeds(queued, tmp_path):
    from pathlib import Path
    root, job = queued
    fake = FakeSglang(statuses=("completed",), truncate_contents=1)
    try:
        code, _ = _run(job, root, tmp_path, fake)
    finally:
        fake.close()
    assert code == 0
    assert Path(job.output_stem + ".mp4").read_bytes() == b"MP4-BYTES"
    assert fake.contents == ["vid-1", "vid-1"]


def test_cancel_while_polling_deletes_and_fails(queued, tmp_path):
    root, job = queued
    fake = FakeSglang(statuses=("queued",))

    def sleep(seconds):
        q.request_cancel(root, job.id, "cancelled_by_user")

    try:
        code, log = _run(job, root, tmp_path, fake, sleep=sleep)
    finally:
        fake.close()
    assert (code, log) == (1, "sglang: id=vid-1\nsglang: cancelled_by_user — H3 досчитает сцену "
                              "впустую, следующая задача начнётся после\n")
    assert fake.deletes == ["vid-1"]
    assert _report(job)["status"] == "cancelled"


def test_a_gate_that_reports_cancel_stops_before_any_post(queued, tmp_path):
    root, job = queued
    fake = FakeSglang()
    try:
        code, log = _run(job, root, tmp_path, fake, gate=lambda j: "cancelled_by_user")
    finally:
        fake.close()
    assert (code, fake.posts) == (1, [])


def test_set_running_fields_refuses_unknown_fields_and_non_running_jobs(queued):
    root, job = queued
    with pytest.raises(ValueError):
        q.set_running_fields(root, job.id, note="x")
    with pytest.raises(q.JobNotRunning):
        q.set_running_fields(root, "nope", engine_ref="x")


def test_a_worker_killed_mid_download_leaves_no_mp4(tmp_path, monkeypatch):
    """The cleanup of a *handled* failure hides non-atomic writing; a kill (BaseException, no
    cleanup runs) is what `queue.reconcile` would meet: a bare `<stem>.mp4` it takes for done."""
    from pathlib import Path

    class _Killed(BaseException):
        pass

    def copy_then_die(src, dst, length):
        dst.write(src.read(3))
        dst.flush()
        raise _Killed()

    fake = FakeSglang(statuses=("completed",))
    fake.ids.append("vid-1")
    dest = tmp_path / "x.mp4"
    monkeypatch.setattr(sg.shutil, "copyfileobj", copy_then_die)
    try:
        with pytest.raises(_Killed):
            sg.SglangClient(fake.url).download("vid-1", dest)
    finally:
        fake.close()
    assert not Path(dest).exists()


def test_a_job_cancelled_before_the_post_never_posts(queued, tmp_path):
    root, job = queued
    q.request_cancel(root, job.id, "cancelled_by_user")
    fake = FakeSglang()
    try:
        code, log = _run(job, root, tmp_path, fake)
    finally:
        fake.close()
    assert (code, log, fake.posts) == (1, "sglang: cancelled_by_user — отменена до начала\n", [])


def test_a_non_404_4xx_on_a_poll_fails_at_once_with_the_body(queued, tmp_path):
    root, job = queued
    fake = FakeSglang(get_error=(422, {"detail": "bad id"}))
    try:
        code, log = _run(job, root, tmp_path, fake)
    finally:
        fake.close()
    assert (code, log) == (1, "sglang: id=vid-1\nsglang отказал (422): bad id\n")
    assert fake.gets == ["vid-1"]   # one poll, no retry
    report = _report(job)
    assert (report["status"], report["http_status"], report["detail"]) == ("rejected", 422, "bad id")


def test_a_completed_scene_is_recorded_with_its_own_steps(tmp_path):
    import json
    root = q.layout(tmp_path / "queue")["root"]
    (tmp_path / "scenes").mkdir()
    ref = tmp_path / "ref.png"
    ref.write_bytes(b"\x89PNG\r\n\x1a\n")
    args = ["generate", "p", "--width", "896", "--height", "512", "--duration", str(175 / 24),
            "--steps", "20", "--tag", "t", "--outdir", str(tmp_path / "scenes"), "--ref", str(ref)]
    q.submit(root, args, "", {"output_stem": str(tmp_path / "scenes" / "h3-t-896x512")}, {})
    job = q.claim(root)
    fake = FakeSglang(statuses=("queued", "completed"))
    try:
        code, _ = _run(job, root, tmp_path, fake, clock=_Clock(10.0, 80.0, 95.0, 110.0))
    finally:
        fake.close()
    assert code == 0
    rows = (tmp_path / "sglang-history.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(r) for r in rows] == \
        [{"width": 896, "height": 512, "frames": 175, "steps": 20, "wall_s": 100.0}]
