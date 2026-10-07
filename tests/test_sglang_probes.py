"""The probe script's payload builders (spec §6): the beach replay must be chain_beach.py's own
payload, with only the step count cut so a probe never costs a 45-minute render; the probe loop
must time the POST itself (the adapter's 60 s timeout question) and keep the server's peak."""
import importlib.util
import sys
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("sglang_probes",
                                               ROOT / "tools" / "probes" / "sglang_probes.py")
probes = importlib.util.module_from_spec(_spec)
sys.modules["sglang_probes"] = probes
_spec.loader.exec_module(probes)


def test_beach_payload_is_chain_beach_head_scene_with_one_step():
    job = {"name": "beach-01", "duration": 7.0, "seed": 42, "short_edge": 512,
           "keyframe": "/home/alex/h3-bench/inputs/pano/beach-h.png",
           "refs": ["/home/alex/h3-bench/inputs/angelina/face_small.jpg"], "prompt": "P"}
    assert probes.beach_payload(job) == {
        "model": "MiniMaxAI/MiniMax-H3", "prompt": "P", "task": "ref2va",
        "conditions": [
            {"type": "image", "uri": "/home/alex/Projects/h3-bench/inputs/pano/beach-h.png",
             "role": "keyframe", "frame_index": 0},
            {"type": "image", "uri": "/home/alex/Projects/h3-bench/inputs/angelina/face_small.jpg",
             "role": "reference"}],
        "target": {"short_edge": 512, "aspect_ratio": "16:9", "duration_seconds": 7.0},
        "num_outputs_per_prompt": 1, "num_inference_steps": 1, "flow_shift": 12.0,
        "audio_flow_shift": 3.0, "seed": 42, "quality": "lossless"}


def test_probe_payloads_cover_the_open_questions(tmp_path):
    payloads = probes.probe_payloads(tmp_path)
    assert sorted(payloads) == ["eight_references", "keyframe_without_reference",
                                "picture_numbering"]
    assert all(p["task"] == "ref2va" for p in payloads.values())
    assert [c["role"] for c in payloads["keyframe_without_reference"]["conditions"]] == ["keyframe"]
    numbering = payloads["picture_numbering"]
    assert [(c["role"], Path(c["uri"]).name) for c in numbering["conditions"]] == [
        ("keyframe", "table.png"), ("reference", "red-cube.png"), ("reference", "blue-ball.png")]
    assert numbering["prompt"] == (
        "subject_definitions:\n"
        "<Subject 1> is the object shown in <Picture 1>.\n"
        "<Subject 2> is the object shown in <Picture 2>.\n\n"
        "On the empty table, <Subject 1> stands at the left edge and <Subject 2> at the right "
        "edge; the camera does not move.")
    assert [c["role"] for c in payloads["eight_references"]["conditions"]] == ["reference"] * 8
    assert all(p["num_inference_steps"] == 8 and p["target"]["duration_seconds"] == 3.0
               for p in payloads.values())


def test_picture_numbering_inputs_are_what_the_owner_is_told_to_look_for(tmp_path):
    """The verdict is read off a frame by eye: red square = Picture 1, blue disc = Picture 2,
    grey = the keyframe. Pixels, not file names, are what the model sees."""
    conditions = probes.probe_payloads(tmp_path)["picture_numbering"]["conditions"]
    table, red, blue = (Image.open(c["uri"]).convert("RGB") for c in conditions)
    assert table.getpixel((256, 256)) == (150, 150, 150)
    assert red.getpixel((256, 256)) == (220, 20, 20) and red.getpixel((10, 10)) == (255, 255, 255)
    assert red.getpixel((130, 130)) == (220, 20, 20)            # a square fills its corners
    assert blue.getpixel((256, 256)) == (20, 40, 220)
    assert blue.getpixel((130, 130)) == (255, 255, 255)         # a disc does not


def test_references_payload_has_n_distinct_cards_each_named_in_order(tmp_path):
    for n in (3, 6, 9):
        payload = probes.references_payload(tmp_path, n)
        uris = [c["uri"] for c in payload["conditions"]]
        assert [c["role"] for c in payload["conditions"]] == ["reference"] * n
        assert len({Image.open(u).convert("RGB").getpixel((0, 0)) for u in uris}) == n
        assert payload["prompt"].splitlines()[1:n + 1] == [
            f"<Subject {i}> is the colour card in <Picture {i}>." for i in range(1, n + 1)]
        assert payload["task"] == "ref2va" and payload["num_inference_steps"] == 8
    assert probes.references_payload(tmp_path, 8) == probes.probe_payloads(tmp_path)[
        "eight_references"]


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class _Client:
    """Each call advances the probe's clock by the time that call 'took' on the server."""

    def __init__(self, clock, *, create_s, gets):
        self.clock, self.create_s, self.gets = clock, create_s, list(gets)
        self.created = []

    def create(self, payload):
        self.created.append(payload)
        self.clock.now += self.create_s
        return {"id": "v1"}

    def get(self, video_id):
        assert video_id == "v1"
        took, status = self.gets.pop(0)
        self.clock.now += took
        return status


def test_run_probe_times_the_post_and_the_slowest_poll_and_keeps_the_servers_peak():
    clock = _Clock()
    client = _Client(clock, create_s=1.25, gets=[
        (0.5, {"status": "queued"}),
        (3.0, {"status": "in_progress"}),
        (0.25, {"status": "completed", "peak_memory_mb": 60416.0, "inference_time_s": 99.5}),
    ])
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        clock.now += seconds

    result = probes.run_probe("p", {"x": 1}, client, poll=10.0, sleep=sleep, clock=clock)
    assert client.created == [{"x": 1}]
    assert slept == [10.0, 10.0]
    assert result == {"probe": "p", "result": "completed", "id": "v1", "create_s": 1.25,
                      "max_get_s": 3.0, "wall_s": 25.0, "peak_memory_mb": 60416.0,
                      "inference_time_s": 99.5, "error": None}


def test_run_probe_reports_a_rejected_post_with_its_detail_and_time():
    clock = _Clock()

    class Rejecting:
        def create(self, payload):
            clock.now += 0.5
            raise probes.sg.SglangHTTPError(400, "needs a reference", "{}")

    assert probes.run_probe("k", {}, Rejecting(), clock=clock) == {
        "probe": "k", "result": "http_error", "status": 400, "detail": "needs a reference",
        "create_s": 0.5}


@pytest.mark.parametrize("name,n", [("references:3", 3), ("references:9", 9)])
def test_named_payload_resolves_reference_counts(tmp_path, name, n):
    payload = probes.named_payload(name, tmp_path, beach_jobs=tmp_path / "absent.json", steps=None)
    assert len(payload["conditions"]) == n


def test_named_payload_steps_override_touches_only_the_step_count(tmp_path):
    plain = probes.named_payload("picture_numbering", tmp_path, beach_jobs=None, steps=None)
    more = probes.named_payload("picture_numbering", tmp_path, beach_jobs=None, steps=20)
    assert more == {**plain, "num_inference_steps": 20}
