"""The probe script's payload builders (spec §6): the beach replay must be chain_beach.py's own
payload, with only the step count cut so a probe never costs a 45-minute render; the probe loop
must time the POST itself (the adapter's 60 s timeout question) and keep the server's peak."""
import importlib.util
import json
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


def test_beach_payload_is_chain_beach_head_scene_with_two_steps():
    """Two, not one: the live server fails a 1-step H3 job ("requires num_inference_steps >= 2
    because its video/audio sigma schedules include both interval endpoints", 2026-10-07)."""
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
        "num_outputs_per_prompt": 1, "num_inference_steps": 2, "flow_shift": 12.0,
        "audio_flow_shift": 3.0, "seed": 42, "quality": "lossless"}


_BASE = {"model": "MiniMaxAI/MiniMax-H3", "num_outputs_per_prompt": 1, "flow_shift": 12.0,
         "audio_flow_shift": 3.0, "seed": 42, "quality": "lossless", "num_inference_steps": 8,
         "task": "ref2va"}
_NUMBERING_PROMPT = (
    "subject_definitions:\n"
    "<Subject 1> is the object shown in <Picture 1>.\n"
    "<Subject 2> is the object shown in <Picture 2>.\n\n"
    "On the empty table, <Subject 1> stands at the left edge and <Subject 2> at the right "
    "edge; the camera does not move.")


def _masked(payload: dict, root: Path) -> dict:
    """The payload with every condition uri made relative to `root`, so a whole payload can be
    compared with `==` -- every key, every value (CLAUDE.md: content, not form)."""
    prefix = str(root) + "/"
    conditions = []
    for condition in payload["conditions"]:
        assert condition["uri"].startswith(prefix), condition["uri"]
        conditions.append({**condition, "uri": condition["uri"][len(prefix):]})
    return {**payload, "conditions": conditions}


def _numbering(folder: str, prompt: str = _NUMBERING_PROMPT) -> dict:
    return {**_BASE, "prompt": prompt,
            "conditions": [
                {"type": "image", "uri": f"{folder}/table.png", "role": "keyframe",
                 "frame_index": 0},
                {"type": "image", "uri": f"{folder}/red-cube.png", "role": "reference"},
                {"type": "image", "uri": f"{folder}/blue-ball.png", "role": "reference"}],
            "target": {"short_edge": 512, "aspect_ratio": "auto", "duration_seconds": 3.0}}


def _cards(folder: str, n: int) -> dict:
    return {**_BASE,
            "prompt": "subject_definitions:\n" + "\n".join(
                f"<Subject {i}> is the colour card in <Picture {i}>." for i in range(1, n + 1))
            + "\n\nThe cards lie on a table.",
            "conditions": [{"type": "image", "uri": f"{folder}/r{i}.png", "role": "reference"}
                           for i in range(n)],
            "target": {"short_edge": 512, "aspect_ratio": "16:9", "duration_seconds": 3.0}}


def test_probe_payloads_are_exactly_the_planned_requests(tmp_path):
    payloads = probes.probe_payloads(tmp_path)
    assert {name: _masked(p, tmp_path) for name, p in payloads.items()} == {
        "picture_numbering": _numbering("picture_numbering"),
        "keyframe_without_reference": {
            **_BASE, "prompt": "The scene continues.",
            "conditions": [{"type": "image", "uri": "keyframe_without_reference/kf.png",
                            "role": "keyframe", "frame_index": 0}],
            "target": {"short_edge": 512, "aspect_ratio": "auto", "duration_seconds": 3.0}},
        "eight_references": _cards("eight_references", 8)}


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
        payload = probes.references_payload(tmp_path / f"n{n}", n)
        assert _masked(payload, tmp_path) == _cards(f"n{n}", n)
        colours = [Image.open(c["uri"]).convert("RGB").getpixel((0, 0))
                   for c in payload["conditions"]]
        assert colours == [((i * 30) % 255, 80, 160) for i in range(n)]


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


@pytest.mark.parametrize("n", [3, 9])
def test_named_payload_resolves_reference_counts(tmp_path, n):
    payload = probes.named_payload(f"references:{n}", tmp_path, beach_jobs=None, steps=None)
    assert _masked(payload, tmp_path) == _cards(f"references-{n}-512x512", n)


def test_named_payload_refuses_an_unknown_probe(tmp_path):
    with pytest.raises(ValueError, match="unknown probe 'picture_numberin'"):
        probes.named_payload("picture_numberin", tmp_path, beach_jobs=None, steps=None)


def test_named_payload_steps_override_touches_only_the_step_count(tmp_path):
    more = probes.named_payload("picture_numbering", tmp_path, beach_jobs=None, steps=20)
    assert _masked(more, tmp_path) == {**_numbering("picture_numbering"),
                                       "num_inference_steps": 20}


def test_mirrored_numbering_differs_from_the_plain_one_only_by_the_sides(tmp_path):
    """The control for spec §6 (a): if the red square follows <Subject 1> to the right edge when
    the prompt swaps the sides, the placement comes from the numbering, not from the order."""
    mirrored = probes.named_payload("picture_numbering_mirrored", tmp_path, beach_jobs=None,
                                    steps=None)
    assert _masked(mirrored, tmp_path) == _numbering(
        "picture_numbering_mirrored", _NUMBERING_PROMPT.replace(
            "<Subject 1> stands at the left edge and <Subject 2> at the right edge",
            "<Subject 1> stands at the right edge and <Subject 2> at the left edge"))


def test_named_payload_duration_override_touches_only_the_duration(tmp_path):
    """Peak memory grows with the clip: the reference-count probe is also run at 10 s."""
    longer = probes.named_payload("references:6", tmp_path, beach_jobs=None, steps=2,
                                  duration=10.0)
    expected = _cards("references-6-512x512", 6)
    assert _masked(longer, tmp_path) == {**expected, "num_inference_steps": 2,
                                         "target": {**expected["target"],
                                                    "duration_seconds": 10.0}}


def test_reference_cards_can_be_portraits(tmp_path):
    """The server scales every reference to a 2048 px short edge with no area cap, so a portrait
    photo costs more tokens than a square card: the memory probe must be able to send one."""
    payload = probes.named_payload("references:2", tmp_path, beach_jobs=None, steps=None,
                                   ref_size=(512, 683))
    assert _masked(payload, tmp_path) == _cards("references-2-512x683", 2)
    assert [Image.open(c["uri"]).size for c in payload["conditions"]] == [(512, 683)] * 2


# -- main(): the card is asked for only after every input is built, and always given back -------

class _Dispatcher:
    def __init__(self, states=("ready",)):
        self.states = list(states)
        self.calls = []

    def acquire(self, engine):
        self.calls.append(("acquire", engine))
        return {"state": self.states.pop(0) if len(self.states) > 1 else self.states[0]}

    def release(self):
        self.calls.append(("release",))
        return {"ok": True}


class _Server:
    """Every job fails at once (no download/ffmpeg); records what was posted."""

    def __init__(self, status="failed"):
        self.posted = []
        self.status = status

    def create(self, payload):
        self.posted.append(payload)
        return {"id": f"v{len(self.posted)}"}

    def get(self, video_id):
        return {"status": self.status}


@pytest.fixture
def out_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(probes, "OUT_DIR", tmp_path / "probes")
    return tmp_path / "probes"


def _no_sleep(seconds):
    raise AssertionError("must not sleep")


@pytest.mark.parametrize("argv,error", [
    (["beach", "--beach-jobs", "/nonexistent/beach-jobs.json"], FileNotFoundError),
    (["picture_numbering", "picture_numberin"], ValueError),
])
def test_a_bad_input_fails_before_the_card_is_asked_for(out_dir, argv, error):
    dispatcher = _Dispatcher()
    with pytest.raises(error):
        probes.main(argv, dispatcher=dispatcher, client=_Server(), sleep=_no_sleep)
    assert dispatcher.calls == []


def test_ctrl_c_while_h3_is_still_starting_gives_the_card_back(out_dir):
    dispatcher = _Dispatcher(states=("starting",))

    def interrupted(seconds):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        probes.main(["picture_numbering"], dispatcher=dispatcher, client=_Server(),
                    sleep=interrupted)
    assert dispatcher.calls == [("acquire", "h3"), ("release",)]


def test_an_engine_that_never_gets_ready_is_given_up_on_and_released(out_dir):
    dispatcher = _Dispatcher(states=("starting",))
    clock = _Clock()

    def sleep(seconds):
        clock.now += seconds

    assert probes.main(["picture_numbering"], dispatcher=dispatcher, client=_Server(),
                       sleep=sleep, clock=clock) == 2
    acquires = int(probes.MAX_ACQUIRE_SECONDS // 10) + 1
    assert dispatcher.calls == [("acquire", "h3")] * acquires + [("release",)]


def test_a_job_that_never_finishes_times_out_and_the_card_is_released(out_dir):
    dispatcher = _Dispatcher()
    clock = _Clock()

    def sleep(seconds):
        clock.now += seconds

    assert probes.main(["references:1"], dispatcher=dispatcher,
                       client=_Server(status="in_progress"), sleep=sleep, clock=clock,
                       max_wait=30.0) == 0
    (line,) = (out_dir).glob("*.jsonl")
    record = json.loads(line.read_text(encoding="utf-8"))
    assert (record["result"], record["error"], record["wall_s"]) == (
        "timeout", "still 'in_progress' after 30 s", 30.0)
    assert dispatcher.calls == [("acquire", "h3"), ("release",)]


def test_a_mixed_run_keeps_each_probes_own_pictures_and_logs_its_parameters(out_dir):
    """--ref-size portraits next to probes with square cards of the same names: the server reads
    the files by path at render time, so the portraits must still be portraits then."""
    server = _Server()
    dispatcher = _Dispatcher()
    assert probes.main(["--ref-size", "512x683", "--steps", "2", "--duration", "10",
                        "references:2", "picture_numbering", "eight_references"],
                       dispatcher=dispatcher, client=server, sleep=_no_sleep) == 0
    refs = [c["uri"] for c in server.posted[0]["conditions"]]
    assert [Image.open(u).size for u in refs] == [(512, 683)] * 2
    assert [Image.open(c["uri"]).size for c in server.posted[2]["conditions"]] == [(512, 512)] * 8
    (jsonl,) = out_dir.glob("*.jsonl")
    records = [json.loads(line) for line in jsonl.read_text(encoding="utf-8").splitlines()]
    assert [r["probe"] for r in records] == ["references:2", "picture_numbering",
                                             "eight_references"]
    assert records[0]["args"] == {"steps": 2, "duration": 10.0, "ref_size": "512x683",
                                  "together": False}
    assert records[0]["request"] == {
        "num_inference_steps": 2,
        "target": {"short_edge": 512, "aspect_ratio": "16:9", "duration_seconds": 10.0},
        "roles": ["reference", "reference"], "reference_sizes": ["512x683", "512x683"]}
    assert records[1]["request"] == {
        "num_inference_steps": 2,
        "target": {"short_edge": 512, "aspect_ratio": "auto", "duration_seconds": 10.0},
        "roles": ["keyframe", "reference", "reference"],
        "reference_sizes": ["512x512", "512x512"]}
    assert dispatcher.calls == [("acquire", "h3"), ("release",)]


def test_the_probes_ask_for_the_card_as_their_own_client(out_dir, monkeypatch):
    """Final review C3: the probes' `finally: release()` stops only what the probes raised."""
    made = []

    class _Recorder:
        def __init__(self, *args, **kwargs):
            made.append(kwargs)

        def acquire(self, engine):
            return {"ok": True, "state": "failed", "reason": "x", "log": "/l"}

        def release(self):
            return {"ok": True, "stopped": []}

    monkeypatch.setattr(probes, "DispatcherClient", _Recorder)
    probes.main(["picture_numbering"], client=_Server(), sleep=_no_sleep)
    assert made == [{"client": "probes"}]
