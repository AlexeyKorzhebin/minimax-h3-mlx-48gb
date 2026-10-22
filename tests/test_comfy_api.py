"""The shared ComfyUI client (studio spec §8.2): the studio's upload and prompt on the wire."""
from __future__ import annotations

import pytest

from _fake_comfy_studio import FakeComfyStudio
from h3_48gb.engines.comfy_api import ComfyApi, ComfyError


@pytest.fixture
def fake(tmp_path):
    comfy = FakeComfyStudio(tmp_path / "out")
    yield comfy
    comfy.close()


def test_upload_image_sends_the_bytes_under_the_given_name(fake):
    api = ComfyApi(fake.url, boundary="B")
    assert api.upload_image(b"\x89PNG-bytes", "k_1-1.png") == "k_1-1.png"
    assert fake.uploads == {"k_1-1.png": b"\x89PNG-bytes"}


def test_submit_prompt_sends_the_whole_payload(fake):
    api = ComfyApi(fake.url)
    graph = {"1": {"class_type": "SaveImage", "inputs": {"filename_prefix": "x"}}}
    fake.hold = True
    prompt_id = api.submit_prompt({"prompt": graph, "client_id": "k_1",
                                   "extra_data": {"studio_task_id": "k_1"}})
    [job] = fake.prompts
    assert (job["prompt_id"], job["prompt"], job["client_id"], job["extra_data"]) == (
        prompt_id, graph, "k_1", {"client_id": "k_1", "studio_task_id": "k_1"})
    api.delete_queued(["nothing"])
    assert fake.deleted == ["nothing"]


def test_a_refused_graph_and_a_dead_engine_are_comfy_errors(fake):
    api = ComfyApi(fake.url)
    with pytest.raises(ComfyError, match=r"ComfyUI POST /prompt: HTTP 400: .*unknown \['Nope'\]"):
        api.submit_prompt({"prompt": {"1": {"class_type": "Nope", "inputs": {}}},
                           "client_id": "k"})
    with pytest.raises(ComfyError, match="ComfyUI GET /queue недоступен"):
        ComfyApi("http://127.0.0.1:9", timeout=1).queue()


def test_models_and_object_info(fake):
    api = ComfyApi(fake.url)
    assert api.models("vae") == ["minimax_h3_audio_vae_fp32.safetensors",
                                 "minimax_h3_video_vae_fp16.safetensors",
                                 "qwen_image_vae.safetensors"]
    assert "MiniMaxH3ImageToVideo" in api.object_info()
    assert api.system_stats()["system"]["comfyui_version"] == "0.42.0"


def test_the_ltx_client_keeps_its_wire_and_its_error(fake, tmp_path):
    """R-S2-2: the old panel's client on the shared base — the same `client_id`, the same
    exception type its callers catch (`ltx.run_upscale` catches UpscaleError)."""
    from h3_48gb.engines import ltx
    client = ltx.ComfyClient(fake.url)
    fake.hold = True
    graph = {"1": {"class_type": "SaveImage", "inputs": {"filename_prefix": "x"}}}
    prompt_id = client.prompt(graph)
    [job] = fake.prompts
    assert (job["prompt_id"], job["client_id"], job["extra_data"]) == (
        prompt_id, "h3-panel", {"client_id": "h3-panel"})
    with pytest.raises(ltx.UpscaleError, match="ComfyUI GET /history/x недоступен"):
        ltx.ComfyClient("http://127.0.0.1:9", timeout=1).history("x")


def test_view_returns_an_uploaded_input(fake):
    api = ComfyApi(fake.url)
    api.upload_image(b"bytes-of-a-frame", "k_2-1.png")
    assert api.view("k_2-1.png") == b"bytes-of-a-frame"
    with pytest.raises(ComfyError, match="ComfyUI GET /view nope.png недоступен"):
        api.view("nope.png")
