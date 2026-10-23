"""The executor contract (studio spec §8.1, §12.2) for the ComfyUI adapter: every declared
capability, on the fake always and on a live ComfyUI when STUDIO_LIVE_COMFY is set.

Live run (alex-neuro, the studio's ComfyUI up under the dispatcher, run where its output folder is
readable): STUDIO_LIVE_COMFY=http://127.0.0.1:8189 STUDIO_LIVE_COMFY_OUTPUT=/home/alex/Outputs/comfy-h3
STUDIO_LIVE_DISPATCHER=http://127.0.0.1:8790 STUDIO_LIVE_TEMPLATES=krea2-t2i[,h3-fl2va,h3-ref2va].

Flat in tests/ on purpose (not tests/contract/): tests/ has no __init__.py and pytest imports in
`prepend` mode, so a subfolder would not see the fakes next to it."""
from __future__ import annotations

import hashlib
import io
import os
import time
from pathlib import Path

import pytest
from PIL import Image

from _fake_comfy_studio import FakeComfyStudio, read_comment
from _fake_dispatcher import FakeDispatcher
from h3_48gb.engines.comfy_api import ComfyApi
from h3_48gb.engines.dispatcher_client import DispatcherClient
from h3_48gb.studio import ids, templates
from h3_48gb.studio.executors.base import ExecutorError, TaskSpec
from h3_48gb.studio.executors.comfy import ComfyExecutor

LIVE = os.environ.get("STUDIO_LIVE_COMFY")
LIVE_TEMPLATES = [t for t in os.environ.get("STUDIO_LIVE_TEMPLATES", "krea2-t2i").split(",") if t]


KREA_PROMPT = "A woman in a linen dress on a forest trail, morning."
H3_PROMPT = "She walks toward the camera and smiles."


def _png(path: Path, rgb: tuple[int, int, int], size=(896, 512)) -> Path:
    Image.new("RGB", size, rgb).save(path)
    return path


def _spec(tmp_path: Path, template: str, *, seed: int = 11, steps: int | None = None,
          task_id: str | None = None) -> TaskSpec:
    task_id = task_id or ids.new_id("k")
    if template == "krea2-t2i":
        values = {"prompt": KREA_PROMPT, "seed": seed,
                  "steps": steps or 8, "width": 1344, "height": 768}
        images = {}
    else:
        values = {"prompt": H3_PROMPT, "seed": seed,
                  "steps": steps or 2, "width": 896, "height": 512, "length": 73}
        if template == "h3-fl2va":
            images = {"first_frame": [_png(tmp_path / f"{task_id}-kf.png", (90, 120, 60))]}
        else:
            images = {"ref_images": [_png(tmp_path / f"{task_id}-r1.png", (200, 40, 40)),
                                     _png(tmp_path / f"{task_id}-r2.png", (40, 40, 200))]}
    return TaskSpec(task_id=task_id, template=template, values=values, images=images,
                    dest_dir=tmp_path / "dest" / task_id)


def _assert_graph(graph: dict, template: str, task_id: str, seed: int, steps: int,
                  names: list[str]) -> None:
    """§12.2: what ComfyUI actually got — values AND links — against literals of this test,
    never against `build_payload` (the code under test, Codex r2 blocker). Shared by the fake
    contract and the live gate."""
    if template == "krea2-t2i":
        assert (graph["4"]["inputs"]["text"], graph["7"]["inputs"]["seed"],
                graph["7"]["inputs"]["steps"], graph["6"]["inputs"]["width"],
                graph["6"]["inputs"]["height"], graph["9"]["inputs"]["filename_prefix"],
                graph["7"]["inputs"]["positive"], graph["7"]["inputs"]["latent_image"],
                graph["8"]["inputs"]["samples"], graph["9"]["inputs"]["images"]) == (
            KREA_PROMPT, seed, steps, 1344, 768, f"studio/{task_id}/f",
            ["4", 0], ["6", 0], ["7", 0], ["8", 0])
        assert names == []
        return
    h3 = graph["6"]["inputs"]
    assert (h3["prompt"], graph["10"]["inputs"]["noise_seed"], graph["7"]["inputs"]["steps"],
            h3["length"], h3["width"], h3["height"], graph["15"]["inputs"]["filename_prefix"],
            graph["11"]["inputs"]["latent_image"], graph["8"]["inputs"]["conditioning"],
            graph["14"]["inputs"]["images"], graph["15"]["inputs"]["video"]) == (
        H3_PROMPT, seed, steps, 73, 896, 512, f"studio/{task_id}/f",
        ["6", 1], ["6", 0], ["12", 0], ["14", 0])
    # the links, not just the nodes: which picture feeds which input, in order (Codex-8)
    if template == "h3-fl2va":
        assert (h3["first_frame"], graph["5"]["inputs"]["image"], len(names)) == (
            ["5", 0], names[0], 1)
    else:
        assert [h3[f"ref_images.ref_image_{i}"] for i in range(len(names))] == \
            [[f"5_{i}", 0] for i in range(len(names))]
        assert [graph[f"5_{i}"]["inputs"]["image"] for i in range(len(names))] == names
        assert sorted(k for k in h3 if k.startswith("ref_images.")) == \
            [f"ref_images.ref_image_{i}" for i in range(len(names))]


def _wait(executor, ref, *, timeout=900.0, poll=0.05):
    seen, deadline = [], time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = executor.status(ref)
        if status["progress"] is not None:
            seen.append(status["progress"])
        if status["state"] in ("done", "failed", "cancelled", "lost"):
            return status, seen
        time.sleep(poll)
    raise AssertionError(f"{ref} did not finish in {timeout} s")


@pytest.fixture
def fake(tmp_path):
    comfy = FakeComfyStudio(tmp_path / "comfy-out")
    yield comfy
    comfy.close()


def _fake_executor(fake, tmp_path, dispatcher=None):
    return ComfyExecutor(url=fake.url, output_dir=fake.output_dir, scope="local-mac",
                         dispatcher=dispatcher)


# -- declared capabilities ---------------------------------------------------------------------

def test_the_adapter_declares_what_spec_8_2_says():
    assert ComfyExecutor.capabilities == {"find": "exact", "restart_evidence": True,
                                          "progress": True, "cancel": "hard",
                                          "async_submit": True}


@pytest.mark.parametrize("template", ["krea2-t2i", "h3-fl2va", "h3-ref2va"])
def test_submit_status_result_and_the_actual_payload(fake, tmp_path, template):
    """§12.2: the graph ComfyUI got holds our prompt, seed, steps, length and the uploaded file
    names; the uploaded bytes are the input files; the result is the engine's file."""
    executor = _fake_executor(fake, tmp_path)
    spec = _spec(tmp_path, template, seed=4242)
    assert executor.probe(spec) == {"alive": True, "ready": True, "detail": "готов"}
    ref = executor.submit(spec)
    status, progress = _wait(executor, ref)
    assert status == {"state": "done", "progress": status["progress"], "error": None}
    steps = spec.values["steps"]
    assert progress == sorted(progress) and progress[-1] == (steps, steps)   # monotone, to the end
    [job] = fake.prompts
    assert (job["prompt_id"], job["client_id"], job["extra_data"]) == (
        ref, spec.task_id, {"client_id": spec.task_id, "studio_task_id": spec.task_id})
    graph = job["prompt"]
    files = [p for paths in spec.images.values() for p in paths]
    names = [f"{spec.task_id}-{n}.png" for n in range(1, len(files) + 1)]
    assert {n: hashlib.sha256(fake.uploads[n]).hexdigest() for n in names} == \
        {n: hashlib.sha256(p.read_bytes()).hexdigest() for n, p in zip(names, files)}
    assert fake.upload_fields == {n: {"type": "input", "overwrite": "true"} for n in names}
    _assert_graph(graph, template, spec.task_id, 4242, 8 if template == "krea2-t2i" else 2,
                  names)
    [out] = executor.result(ref, spec.dest_dir)
    expected = "f_00001_.png" if template == "krea2-t2i" else "f_00001_.mp4"
    assert out == spec.dest_dir / expected
    assert out.read_bytes() == (fake.output_dir / "studio" / spec.task_id / expected).read_bytes()
    if template != "krea2-t2i":
        assert read_comment(out)["pictures"] == [
            {"name": n, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
            for n, p in zip(names, files)]


def test_find_locates_a_task_whose_prompt_id_was_lost(fake, tmp_path):
    """The client died right after POST /prompt (GPU-DAY-21 п.6): find() by extra_data in the
    queue, then in the history, then — after a restart — by the files."""
    executor = _fake_executor(fake, tmp_path)
    fake.hold = True
    spec = _spec(tmp_path, "h3-fl2va")
    ref = executor.submit(spec)
    assert executor.find(spec) == ref                     # in /queue (running)
    other = _spec(tmp_path, "h3-fl2va")
    assert executor.find(other) is None
    fake.release()
    _wait(executor, ref)
    assert executor.find(spec) == ref                     # in /history
    fake.restart()
    assert executor.status(ref)["state"] == "lost"
    assert executor.find(spec) == f"file:{spec.task_id}"  # history gone, files stay
    assert executor.status(f"file:{spec.task_id}")["state"] == "done"
    assert [p.name for p in executor.result(f"file:{spec.task_id}", spec.dest_dir)] == \
        ["f_00001_.mp4"]


def test_a_task_killed_by_a_restart_is_not_found(fake, tmp_path):
    executor = _fake_executor(fake, tmp_path)
    fake.hold = True
    spec = _spec(tmp_path, "h3-fl2va")
    ref = executor.submit(spec)
    fake.restart()
    assert (executor.status(ref)["state"], executor.find(spec)) == ("lost", None)


def test_restarted_since_reads_the_dispatchers_started_at(tmp_path, fake):
    dispatcher = FakeDispatcher(own={"comfy-studio": {"pid": 1, "owner": "studio-worker",
                                                      "started_at": 1000.0, "ready": True}})
    try:
        executor = _fake_executor(fake, tmp_path,
                                  DispatcherClient(dispatcher.url, client="studio-worker"))
        assert (executor.restarted_since(999.0), executor.restarted_since(1000.0),
                executor.restarted_since(1001.0)) == (True, False, False)
        dispatcher.own = {}
        assert executor.restarted_since(1001.0) is True          # no live record: queue lost
    finally:
        dispatcher.close()
    assert _fake_executor(fake, tmp_path).restarted_since(0.0) is None   # no dispatcher: unknown
    gone = _fake_executor(fake, tmp_path, DispatcherClient("http://127.0.0.1:9", timeout=0.5))
    assert gone.restarted_since(0.0) is None


def test_hard_cancel_of_a_running_task_stops_it_and_frees_the_queue(fake, tmp_path):
    executor = _fake_executor(fake, tmp_path)
    fake.hold = True
    spec = _spec(tmp_path, "h3-fl2va")
    ref = executor.submit(spec)
    deadline = time.monotonic() + 5
    while executor.status(ref)["state"] != "running" and time.monotonic() < deadline:
        time.sleep(0.01)
    executor.cancel(ref)
    status, _ = _wait(executor, ref)
    assert (status["state"], fake.interrupts) == ("cancelled", 1)
    assert fake.interrupt_bodies == [{"prompt_id": ref}]      # only our prompt, not whatever runs
    assert ComfyApi(fake.url).queue() == {"queue_running": [], "queue_pending": []}
    assert not (fake.output_dir / "studio" / spec.task_id).exists()


def test_cancel_of_a_waiting_task_deletes_it_from_the_queue(fake, tmp_path):
    executor = _fake_executor(fake, tmp_path)
    fake.hold = True
    first, second = _spec(tmp_path, "h3-fl2va"), _spec(tmp_path, "h3-fl2va")
    executor.submit(first)
    ref = executor.submit(second)
    executor.cancel(ref)
    assert (fake.deleted, fake.interrupts) == ([ref], 0)
    fake.release()
    fake.wait_idle()
    assert executor.status(ref)["state"] == "lost"


def test_probe_names_what_is_missing(fake, tmp_path):
    executor = _fake_executor(fake, tmp_path)
    spec = _spec(tmp_path, "h3-ref2va")
    fake.classes.remove("MiniMaxH3ReferenceToVideo")
    assert executor.probe(spec) == {"alive": True, "ready": False,
                                    "detail": "в ComfyUI нет узлов: MiniMaxH3ReferenceToVideo"}
    fake.classes.append("MiniMaxH3ReferenceToVideo")
    fake.models["diffusion_models"].remove("minimax_h3_ref2va_pruned_int8_convrot.safetensors")
    assert executor.probe(spec) == {
        "alive": True, "ready": False,
        "detail": "в ComfyUI нет моделей diffusion_models: "
                  "minimax_h3_ref2va_pruned_int8_convrot.safetensors"}
    fake.models["diffusion_models"].append("minimax_h3_ref2va_pruned_int8_convrot.safetensors")
    fake.foreign_running = True
    assert executor.probe(spec) == {"alive": True, "ready": False,
                                    "detail": "в очереди ComfyUI чужие задачи: 1"}
    dead = ComfyExecutor(url="http://127.0.0.1:9", output_dir=tmp_path, scope="local-mac")
    probe = dead.probe(spec)
    assert (probe["alive"], probe["ready"]) == (False, False)


def test_an_engine_error_is_failed_with_its_text(tmp_path):
    comfy = FakeComfyStudio(tmp_path / "out", fail="CUDA out of memory")
    try:
        executor = _fake_executor(comfy, tmp_path)
        status, _ = _wait(executor, executor.submit(_spec(tmp_path, "h3-fl2va")))
        assert status == {"state": "failed", "progress": None,
                          "error": "ComfyUI: CUDA out of memory"}
    finally:
        comfy.close()


def test_a_template_error_never_reaches_the_engine(fake, tmp_path):
    executor = _fake_executor(fake, tmp_path)
    spec = _spec(tmp_path, "h3-ref2va")
    seven = TaskSpec(task_id=spec.task_id, template="h3-ref2va", values=spec.values,
                     images={"ref_images": [spec.images["ref_images"][0]] * 7},
                     dest_dir=spec.dest_dir)
    with pytest.raises(ExecutorError, match="7 images > limit 6 at 73 frames"):
        executor.submit(seven)
    assert fake.prompts == []


def test_a_dropped_socket_costs_the_progress_not_the_result(tmp_path):
    comfy = FakeComfyStudio(tmp_path / "out", drop_ws=True)
    try:
        executor = _fake_executor(comfy, tmp_path)
        status, progress = _wait(executor, executor.submit(_spec(tmp_path, "h3-fl2va")))
        assert (status["state"], progress) == ("done", [])
    finally:
        comfy.close()


# -- live -------------------------------------------------------------------------------------

@pytest.mark.skipif(not LIVE, reason="STUDIO_LIVE_COMFY not set")
@pytest.mark.parametrize("template", LIVE_TEMPLATES)
def test_live_contract(tmp_path, template):
    output = Path(os.environ["STUDIO_LIVE_COMFY_OUTPUT"])
    disp = os.environ.get("STUDIO_LIVE_DISPATCHER")
    executor = ComfyExecutor(url=LIVE, output_dir=output, scope="live",
                             dispatcher=DispatcherClient(disp, client="studio-worker")
                             if disp else None)
    spec = _spec(tmp_path, template, seed=4242)
    probe = executor.probe(spec)
    assert probe == {"alive": True, "ready": True, "detail": "готов"}, probe
    began = time.time()
    ref = executor.submit(spec)
    assert executor.find(spec) == ref
    status, progress = _wait(executor, ref)
    assert status["state"] == "done", status
    steps = spec.values["steps"]
    assert progress and progress == sorted(progress) and progress[-1] == (steps, steps)
    api = ComfyApi(LIVE)
    item = api.history(ref)
    graph, extra = item["prompt"][2], item["prompt"][3]
    assert extra["studio_task_id"] == spec.task_id
    files = [p for paths in spec.images.values() for p in paths]
    names = [f"{spec.task_id}-{n}.png" for n in range(1, len(files) + 1)]
    # every input of §12.2, values and links, against this test's literals (not build_payload)
    _assert_graph(graph, template, spec.task_id, 4242, steps, names)
    for n, path in enumerate(files, start=1):
        assert api.view(f"{spec.task_id}-{n}.png") == path.read_bytes()
    files = executor.result(ref, spec.dest_dir)
    assert [f.parent for f in files] == [spec.dest_dir]
    if disp:
        assert executor.restarted_since(began) is False
    if template != "krea2-t2i":
        import subprocess
        frames = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams",
                                 "v:0", "-show_entries", "stream=nb_read_frames", "-of",
                                 "csv=p=0", str(files[0])], capture_output=True, text=True)
        assert int(frames.stdout.strip()) == 73
    cancel_spec = _spec(tmp_path, template, seed=7)
    cref = executor.submit(cancel_spec)
    while executor.status(cref)["state"] == "queued":
        time.sleep(0.5)
    executor.cancel(cref)
    status, _ = _wait(executor, cref, timeout=120)
    assert status["state"] == "cancelled"
    assert ComfyApi(LIVE).queue()["queue_running"] == []


def test_a_refused_graph_is_an_error_and_a_lost_answer_is_uncertain(fake, tmp_path):
    """§8.5: HTTP 4xx on POST /prompt — ComfyUI refused it, it never runs: `failed`. No answer at
    all — it may have been accepted: `submit_unknown`, never a blind retry."""
    from h3_48gb.studio.executors.base import SubmitUncertain
    executor = _fake_executor(fake, tmp_path)
    fake.classes.remove("KSampler")
    with pytest.raises(ExecutorError) as refused:
        executor.submit(_spec(tmp_path, "krea2-t2i"))
    assert type(refused.value) is ExecutorError
    assert "HTTP 400" in str(refused.value)
    dead = ComfyExecutor(url="http://127.0.0.1:9", output_dir=tmp_path, scope="local-mac")
    with pytest.raises(SubmitUncertain, match="ComfyUI POST /prompt недоступен"):
        dead.submit(_spec(tmp_path, "krea2-t2i"))


def test_find_that_cannot_ask_the_engine_says_so(tmp_path):
    """«Could not check» is not «not found»: without files, an unreachable engine raises."""
    dead = ComfyExecutor(url="http://127.0.0.1:9", output_dir=tmp_path, scope="local-mac")
    spec = _spec(tmp_path, "krea2-t2i")
    with pytest.raises(ExecutorError, match="очередь и история ComfyUI недоступны"):
        dead.find(spec)
    done = tmp_path / "studio" / spec.task_id
    done.mkdir(parents=True)
    (done / "f_00001_.png").write_bytes(b"x")
    assert dead.find(spec) == f"file:{spec.task_id}"


def test_a_spec_planned_on_another_template_version_is_refused(fake, tmp_path):
    """Codex-10: the executor runs the graph whose sha256 was recorded, or nothing."""
    executor = _fake_executor(fake, tmp_path)
    spec = _spec(tmp_path, "krea2-t2i")
    stale = TaskSpec(**(spec.model_dump() | {"template_sha256": "0" * 64}))
    with pytest.raises(ExecutorError, match=r"^шаблон krea2-t2i изменился после постановки "
                                            r"\(000000000000 → "):
        executor.submit(stale)
    assert fake.prompts == []
    current = TaskSpec(**(spec.model_dump() | {"template_sha256": templates.load("krea2-t2i").sha256}))
    assert _wait(executor, executor.submit(current))[0]["state"] == "done"


def test_engine_version_is_comfyui_torch_and_the_gpu_as_the_engine_says(tmp_path):
    """Spec rev 3.2, И15: «ComfyUI <ver> · torch <ver> · <GPU>» from /system_stats — the whole
    string, exactly; what the engine does not say is left out, an unreachable engine is None."""
    comfy = FakeComfyStudio(tmp_path / "out", comfyui_version="0.42.0",
                            torch_version="2.9.1+cu128",
                            gpu="cuda:0 NVIDIA CMP 170HX 64GB : cudaMallocAsync")
    try:
        executor = _fake_executor(comfy, tmp_path)
        assert executor.engine_version() == (
            "ComfyUI 0.42.0 · torch 2.9.1+cu128 · cuda:0 NVIDIA CMP 170HX 64GB : cudaMallocAsync")
        comfy.torch_version = None
        assert executor.engine_version() == \
            "ComfyUI 0.42.0 · cuda:0 NVIDIA CMP 170HX 64GB : cudaMallocAsync"
        comfy.gpu = ""
        assert executor.engine_version() == "ComfyUI 0.42.0"
        comfy.comfyui_version = None
        assert executor.engine_version() is None
    finally:
        comfy.close()
    dead = ComfyExecutor(url="http://127.0.0.1:9", output_dir=tmp_path, scope="local-mac")
    assert dead.engine_version() is None


def test_gpu_scope_and_estimate_come_from_the_scope_and_the_card(fake, tmp_path):
    executor = ComfyExecutor(url=fake.url, output_dir=fake.output_dir, scope="live-scope")
    spec = _spec(tmp_path, "h3-fl2va")
    assert (executor.gpu_scope(spec), executor.estimate_s(spec)) == (
        "live-scope", templates.load("h3-fl2va").estimate_s)


def test_a_task_that_the_engine_runs_past_the_history_is_failed_after_twelve_odd_polls(
        fake, tmp_path):
    """`/history` entry with no final status: tolerated 11 reads, the 12th is `failed`."""
    executor = _fake_executor(fake, tmp_path)
    fake.history["odd"] = {"prompt": [1, "odd", {}, {}, []], "outputs": {},
                           "status": {"status_str": "success", "completed": False,
                                      "messages": []}}
    states = [executor.status("odd") for _ in range(12)]
    assert [s["state"] for s in states] == ["running"] * 11 + ["failed"]
    assert states[-1]["error"] == "ComfyUI: запись /history без итогового статуса после 12 опросов"


def test_result_names_a_missing_file_and_an_empty_output(fake, tmp_path):
    executor = _fake_executor(fake, tmp_path)
    fake.history["gone"] = {"prompt": [1, "gone", {}, {"studio_task_id": "x"}, []], "status": {
        "status_str": "success", "completed": True, "messages": []},
        "outputs": {"9": {"images": [{"filename": "nope.png", "subfolder": "studio/x",
                                      "type": "output"}]}}}
    with pytest.raises(ExecutorError) as missing:
        executor.result("gone", tmp_path / "d1")
    assert str(missing.value) == \
        f"ComfyUI назвал файл, которого нет: {fake.output_dir}/studio/x/nope.png"
    fake.history["empty"] = {"prompt": [2, "empty", {}, {"studio_task_id": "x"}, []], "outputs": {}, "status": {
        "status_str": "success", "completed": True, "messages": []}}
    with pytest.raises(ExecutorError) as empty:
        executor.result("empty", tmp_path / "d2")
    assert str(empty.value) == "ComfyUI не отдал ни одного файла"


def test_a_lost_answer_after_the_post_is_uncertain_and_the_task_is_found_by_find(tmp_path):
    """The prompt was accepted but the answer came after the client gave up: SubmitUncertain,
    and `find` locates the task by its extra_data (the way S5 resolves submit_unknown)."""
    from h3_48gb.engines.comfy_api import ComfyApi as Api
    from h3_48gb.studio.executors.base import SubmitUncertain
    comfy = FakeComfyStudio(tmp_path / "out", post_timeout=1.5, hold=True)
    try:
        executor = ComfyExecutor(url=comfy.url, output_dir=comfy.output_dir, scope="x",
                                 api=Api(comfy.url, timeout=0.4))
        spec = _spec(tmp_path, "krea2-t2i")
        with pytest.raises(SubmitUncertain):
            executor.submit(spec)
        [job] = comfy.prompts
        assert executor.find(spec) == job["prompt_id"]
    finally:
        comfy.release()
        comfy.close()


def test_the_socket_follows_this_task_only_and_stops_at_its_end(fake, tmp_path):
    """The connection is `/ws?clientId=<task_id>` and is closed by the adapter when the prompt
    ends — no socket is left per finished task."""
    connected = []

    def spy(url, **kwargs):
        from websockets.sync.client import connect
        connected.append((url, kwargs))
        return connect(url, **kwargs)

    executor = ComfyExecutor(url=fake.url, output_dir=fake.output_dir, scope="x", ws_connect=spy)
    spec = _spec(tmp_path, "krea2-t2i")
    ref = executor.submit(spec)
    _wait(executor, ref)
    assert connected == [(f"ws://{fake.url[len('http://'):]}/ws?clientId={spec.task_id}",
                          {"open_timeout": 5, "proxy": None})]
    deadline = time.monotonic() + 5
    while any(t.name == f"comfy-ws-{spec.task_id}" for t in __import__("threading").enumerate()):
        assert time.monotonic() < deadline, "the socket thread did not stop"
        time.sleep(0.01)


def test_a_finished_task_that_shows_in_the_history_only_on_the_second_read_is_done(tmp_path):
    """Between the first /history read and /queue the prompt finished: the second read finds it."""
    comfy = FakeComfyStudio(tmp_path / "out", history_pending=1)
    try:
        executor = _fake_executor(comfy, tmp_path)
        ref = executor.submit(_spec(tmp_path, "krea2-t2i"))
        comfy.wait_idle()
        assert executor.status(ref)["state"] == "done"
    finally:
        comfy.close()


# -- review round 1 ---------------------------------------------------------------------------

def _ws_threads(task_id: str) -> list[str]:
    import threading
    return [t.name for t in threading.enumerate() if t.name == f"comfy-ws-{task_id}"]


@pytest.fixture(autouse=True)
def _long_socket_idle(monkeypatch):
    """The sockets' own exit (the safety net) must not be what ends them in these tests."""
    import h3_48gb.studio.executors.comfy as module
    monkeypatch.setattr(module, "WS_IDLE_S", 60.0)


def _wait_no_socket(task_id: str, timeout: float = 5.0) -> list[str]:
    deadline = time.monotonic() + timeout
    while _ws_threads(task_id) and time.monotonic() < deadline:
        time.sleep(0.02)
    return _ws_threads(task_id)


def test_no_socket_thread_outlives_a_refused_graph(fake, tmp_path):
    executor = _fake_executor(fake, tmp_path)
    fake.classes.remove("KSampler")
    spec = _spec(tmp_path, "krea2-t2i")
    with pytest.raises(ExecutorError):
        executor.submit(spec)
    assert _wait_no_socket(spec.task_id) == []


def test_no_socket_thread_outlives_a_lost_answer(tmp_path):
    from h3_48gb.studio.executors.base import SubmitUncertain
    comfy = FakeComfyStudio(tmp_path / "out")
    try:
        executor = ComfyExecutor(url=comfy.url, output_dir=comfy.output_dir, scope="x",
                                 api=ComfyApi(comfy.url, timeout=5.0))
        comfy.prompt_status = 503
        spec = _spec(tmp_path, "krea2-t2i")
        with pytest.raises(SubmitUncertain):
            executor.submit(spec)
        assert _wait_no_socket(spec.task_id) == []
    finally:
        comfy.close()


def test_no_socket_thread_outlives_a_task_taken_out_of_the_queue(fake, tmp_path):
    executor = _fake_executor(fake, tmp_path)
    fake.hold = True
    first, second = _spec(tmp_path, "krea2-t2i"), _spec(tmp_path, "krea2-t2i")
    first_ref = executor.submit(first)
    ref = executor.submit(second)
    assert _ws_threads(second.task_id) == [f"comfy-ws-{second.task_id}"]
    executor.cancel(ref)
    assert _wait_no_socket(second.task_id) == []
    assert _ws_threads(first.task_id) == [f"comfy-ws-{first.task_id}"]     # the running one stays
    executor.cancel(first_ref)
    fake.release()
    assert _wait_no_socket(first.task_id) == []


def test_a_socket_whose_prompt_is_nowhere_ends_by_itself(tmp_path, monkeypatch):
    """Safety net: the engine restarted (queue and history empty) and sent nothing more."""
    import h3_48gb.studio.executors.comfy as module
    monkeypatch.setattr(module, "WS_IDLE_S", 0.2)     # after the autouse fixture: this one wins
    comfy = FakeComfyStudio(tmp_path / "out", hold=True)
    try:
        executor = _fake_executor(comfy, tmp_path)
        spec = _spec(tmp_path, "krea2-t2i")
        executor.submit(spec)
        comfy.history.clear()
        with comfy._lock:
            comfy._pending.clear()
            comfy._running = None
        comfy.release()
        assert _wait_no_socket(spec.task_id) == []
    finally:
        comfy.close()


def test_the_socket_is_open_before_the_prompt_is_posted(fake, tmp_path):
    """A socket opened after the POST would miss the first steps."""
    seen = []

    def spy(url, **kwargs):
        from websockets.sync.client import connect
        seen.append(len(fake.prompts))
        return connect(url, **kwargs)

    executor = ComfyExecutor(url=fake.url, output_dir=fake.output_dir, scope="x", ws_connect=spy)
    _wait(executor, executor.submit(_spec(tmp_path, "krea2-t2i")))
    assert seen == [0]


def _history_entry(task_id, filename, subfolder):
    return {"prompt": [1, "p", {}, {"studio_task_id": task_id}, []],
            "status": {"status_str": "success", "completed": True, "messages": []},
            "outputs": {"9": {"images": [{"filename": filename, "subfolder": subfolder,
                                          "type": "output"}]}}}


def test_result_copies_only_files_of_the_tasks_own_folder(fake, tmp_path):
    executor = _fake_executor(fake, tmp_path)
    (tmp_path / "comfy-out").mkdir(exist_ok=True)
    (fake.output_dir.parent / "secret.txt").write_text("SECRET")
    other = fake.output_dir / "studio" / "OTHER"
    other.mkdir(parents=True)
    (other / "f_00001_.png").write_bytes(b"other")
    own = fake.output_dir / "studio" / "mine"
    own.mkdir(parents=True)
    (own / "f_00001_.png").write_bytes(b"mine")
    fake.history["dots"] = _history_entry("mine", "secret.txt", "..")
    fake.history["absolute"] = _history_entry("mine", "secret.txt", str(fake.output_dir.parent))
    fake.history["slash"] = _history_entry("mine", "../../secret.txt", "studio/mine")
    fake.history["foreign"] = _history_entry("mine", "f_00001_.png", "studio/OTHER")
    fake.history["noid"] = {**_history_entry("mine", "f_00001_.png", "studio/mine"),
                            "prompt": [1, "p", {}, {}, []]}
    (fake.output_dir / "top.png").write_bytes(b"top")
    fake.history["updir"] = _history_entry("..", "top.png", "")          # task id that is the root
    fake.history["nested"] = _history_entry("a/b", "f.png", "studio/a/b")
    for ref in ("dots", "absolute", "slash", "foreign", "updir", "nested"):
        with pytest.raises(ExecutorError, match="^файл вне папки задачи (mine|\\.\\.|a/b): "):
            executor.result(ref, tmp_path / f"dest-{ref}")
        assert not (tmp_path / f"dest-{ref}").exists()
    with pytest.raises(ExecutorError, match="^в записи ComfyUI нет studio_task_id"):
        executor.result("noid", tmp_path / "dest-noid")
    fake.history["ok"] = _history_entry("mine", "f_00001_.png", "studio/mine")
    [copied] = executor.result("ok", tmp_path / "dest-ok")
    assert (copied, copied.read_bytes()) == (tmp_path / "dest-ok" / "f_00001_.png", b"mine")
    with pytest.raises(ExecutorError, match="^файл вне папки задачи"):
        executor.result("file:../OTHER", tmp_path / "dest-file")


def test_status_of_an_unreachable_engine_is_an_error_not_a_loss(tmp_path):
    dead = ComfyExecutor(url="http://127.0.0.1:9", output_dir=tmp_path, scope="x")
    with pytest.raises(ExecutorError, match="недоступен"):
        dead.status("some-prompt")


def test_find_sees_a_task_that_waits_in_the_queue(fake, tmp_path):
    executor = _fake_executor(fake, tmp_path)
    fake.hold = True
    first, second = _spec(tmp_path, "krea2-t2i"), _spec(tmp_path, "krea2-t2i")
    executor.submit(first)
    ref = executor.submit(second)
    assert [e[1] for e in ComfyApi(fake.url).queue()["queue_pending"]] == [ref]
    assert executor.find(second) == ref
    fake.release()


def test_a_5xx_on_post_is_uncertain_and_a_4xx_is_refused(tmp_path):
    from h3_48gb.studio.executors.base import SubmitUncertain
    comfy = FakeComfyStudio(tmp_path / "out")
    try:
        executor = _fake_executor(comfy, tmp_path)
        comfy.prompt_status = 503
        with pytest.raises(SubmitUncertain, match="HTTP 503"):
            executor.submit(_spec(tmp_path, "krea2-t2i"))
        comfy.prompt_status = 429
        with pytest.raises(ExecutorError, match="HTTP 429") as refused:
            executor.submit(_spec(tmp_path, "krea2-t2i"))
        assert type(refused.value) is ExecutorError
        assert comfy.prompts == []
    finally:
        comfy.close()


def test_comfy_error_carries_the_http_status(fake):
    with pytest.raises(Exception) as err:
        ComfyApi(fake.url)._open("GET", "/nothing")
    assert (type(err.value).__name__, err.value.status) == ("ComfyError", 404)
    with pytest.raises(Exception) as down:
        ComfyApi("http://127.0.0.1:9", timeout=0.5).queue()
    assert down.value.status is None


def test_progress_and_odd_counters_are_dropped_when_the_task_is_collected(fake, tmp_path):
    executor = _fake_executor(fake, tmp_path)
    spec = _spec(tmp_path, "krea2-t2i")
    ref = executor.submit(spec)
    status, _ = _wait(executor, ref)
    assert executor.progress(ref) == (8, 8)
    executor.result(ref, spec.dest_dir)
    assert executor.progress(ref) is None
    fake.history["odd"] = {"prompt": [1, "odd", {}, {}, []], "outputs": {},
                           "status": {"status_str": "success", "completed": False,
                                      "messages": []}}
    assert [executor.status("odd")["state"] for _ in range(12)][-1] == "failed"
    assert executor._odd == {}
    failed = FakeComfyStudio(tmp_path / "out2", fail="boom")
    try:
        ex2 = _fake_executor(failed, tmp_path)
        ref2 = ex2.submit(_spec(tmp_path, "krea2-t2i"))
        assert _wait(ex2, ref2)[0]["state"] == "failed"
        assert ex2.progress(ref2) is None
    finally:
        failed.close()
