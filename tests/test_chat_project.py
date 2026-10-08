"""Wave 1.5, spec §5.4: the chat of a project, chat-made projects on sglang's own duration range
with the session's tags pinned, the duration range in the model's context, and an honest
`shares_gpu` per provider."""
import json
from pathlib import Path

import pytest

from h3_48gb import library as lib
from h3_48gb import project as p
from h3_48gb import provider
from test_chat_web import _serve, fake_llama  # noqa: F401  (fixtures)

PNG = b"\x89PNG\r\n\x1a\n"


def _card(root):
    (root / "uploads").mkdir(exist_ok=True)
    (root / "uploads" / "f.png").write_bytes(PNG)
    return lib.create_card(root, tag="@amazon", kind="person", description="an armored amazon",
                           assets=[root / "uploads" / "f.png"])


def _session_with(srv, project, tags=None):
    sid = srv.post_json("/api/chat", {"source": {"kind": "new"}, "prompt": ""})["id"]
    path = Path(srv.root) / "chat" / f"{sid}.json"
    session = json.loads(path.read_text(encoding="utf-8"))
    session.update({"project": project, "kind": project["kind"]})
    if tags is not None:
        session["tags"] = tags
    path.write_text(json.dumps(session), encoding="utf-8")
    return sid


def test_a_project_chat_starts_with_the_projects_tags(_serve, monkeypatch):  # noqa: F811
    monkeypatch.setenv("H3_ENGINE", "sglang")
    srv = _serve()
    _card(srv.root)
    proj = p.create_project(srv.root, "video", "Бой")
    proj.set_references([{"tag": "@amazon", "version": 1}])
    sid = srv.post_json("/api/chat", {"source": {"kind": "project", "id": proj.id}})["id"]
    session = srv.get_json(f"/api/chat/{sid}")
    assert (session["source"], session["tags"], session["kind"]) == (
        {"kind": "project", "id": proj.id}, ["@amazon"], "video")


def test_a_project_chat_for_a_missing_project_is_refused(_serve):  # noqa: F811
    srv = _serve()
    status, payload = srv.post_json_raw("/api/chat", {"source": {"kind": "project", "id": "nope"}})
    assert (status, payload["error"]["code"]) == (404, "project_not_found")
    assert list((Path(srv.root) / "chat").glob("*.json")) == []


def test_a_project_chat_tells_the_model_it_is_a_video(_serve, fake_llama, monkeypatch):  # noqa: F811
    monkeypatch.setenv("H3_ENGINE", "sglang")
    srv = _serve(providers_port=fake_llama.port)
    proj = p.create_project(srv.root, "video", "Бой")
    sid = srv.post_json("/api/chat", {"source": {"kind": "project", "id": proj.id}})["id"]
    srv.post_json(f"/api/chat/{sid}/message", {"text": "ролик", "prompt": ""})
    system = fake_llama.requests[-1]["body"]["messages"][0]["content"]
    assert "\nkind: video\n" in system


def test_a_chat_made_video_takes_sglang_durations_and_pins_the_tags(_serve, monkeypatch):  # noqa: F811
    monkeypatch.setenv("H3_ENGINE", "sglang")
    srv = _serve()
    _card(srv.root)
    sid = _session_with(srv, {"kind": "video", "scenes": [
        {"prompt": "@amazon walks", "duration": 12}, {"prompt": "@amazon runs", "duration": 3}]},
        tags=["@amazon"])
    pid = srv.post_json("/api/projects", {"session_id": sid})["id"]
    loaded = p.load_project(Path(srv.root) / "projects" / pid)
    assert ([s["duration"] for s in loaded.scenes], loaded.references) == (
        [12.0, 3.0], [{"tag": "@amazon", "version": 1}])


def test_a_chat_made_video_with_an_unknown_tag_is_refused(_serve, monkeypatch):  # noqa: F811
    monkeypatch.setenv("H3_ENGINE", "sglang")
    srv = _serve()
    sid = _session_with(srv, {"kind": "video", "scenes": [{"prompt": "@ghost", "duration": 5}]},
                        tags=["@ghost"])
    status, payload = srv.post_json_raw("/api/projects", {"session_id": sid})
    assert (status, payload["error"]["code"]) == (404, "library_card_not_found")
    assert list((Path(srv.root) / "projects").glob("*")) == []


def test_mlx_keeps_five_to_ten(_serve):  # noqa: F811
    srv = _serve()
    sid = _session_with(srv, {"kind": "video", "scenes": [{"prompt": "a cat", "duration": 12}]})
    status, payload = srv.post_json_raw("/api/projects", {"session_id": sid})
    assert (status, payload["error"]["code"]) == (400, "args_invalid")


@pytest.mark.parametrize("engine, line", [("sglang", "scene duration: 3–15 s"),
                                          ("mlx", "scene duration: 5–10 s")])
def test_the_model_is_told_the_scene_duration_range(_serve, fake_llama, monkeypatch, engine, line):  # noqa: F811
    monkeypatch.setenv("H3_ENGINE", engine)
    srv = _serve(providers_port=fake_llama.port)
    sid = srv.post_json("/api/chat", {"source": {"kind": "new"}, "prompt": ""})["id"]
    srv.post_json(f"/api/chat/{sid}/message", {"text": "ролик", "prompt": ""})
    system = fake_llama.requests[-1]["body"]["messages"][0]["content"]
    assert "\nduration: 10 s\n" + line + "\n" in system


@pytest.mark.parametrize("cfg, expected", [
    ({"type": "llama-local"}, True),
    ({"type": "openai", "base_url": "http://127.0.0.1:8000"}, True),
    ({"type": "openai", "base_url": "http://localhost:8000/v1"}, True),
    ({"type": "openai", "base_url": "http://[::1]:8000"}, True),
    ({"type": "openai", "base_url": "http://host.docker.internal:30000"}, True),
    ({"type": "openai", "base_url": "https://caila.io/api"}, None),
    ({"type": "openai", "base_url": "http://192.168.100.50:8000", "shares_gpu": True}, True),
    ({"type": "llama-local", "shares_gpu": False}, False),
])
def test_shares_gpu(cfg, expected):
    assert provider.shares_gpu(cfg) is expected


def test_providers_carry_shares_gpu(_serve, fake_llama):  # noqa: F811
    srv = _serve(providers_port=fake_llama.port)
    body = srv.get_json("/api/providers")
    assert [(row["name"], row["shares_gpu"]) for row in body["providers"]] == [
        ("qwen-local", True)]


def _scene_duration(schema):
    return schema["schema"]["properties"]["project"]["properties"]["scenes"]["items"][
        "properties"]["duration"]


@pytest.mark.parametrize("engine, low, high", [("sglang", 3, 15), ("mlx", 5, 10)])
def test_the_grammar_and_the_system_prompt_follow_the_engine(monkeypatch, engine, low, high):
    monkeypatch.setenv("H3_ENGINE", engine)
    assert _scene_duration(provider.prompt_schema()) == {
        "type": "number", "minimum": low, "maximum": high}
    text = provider.system_prompt()
    assert (f"Each scene is\n**{low} to {high} seconds** long (`duration`) — split a longer idea "
            f"into more scenes rather than writing\none scene past {high} seconds; the pipeline "
            f"generates and stitches one clip per scene, and {high} seconds\nis the ceiling") in text
    assert "@@" not in text


def test_the_module_schema_constant_is_untouched_by_the_engine(monkeypatch):
    monkeypatch.setenv("H3_ENGINE", "sglang")
    provider.prompt_schema()
    assert _scene_duration(provider.PROMPT_SCHEMA) == {
        "type": "number", "minimum": 5, "maximum": 10}


def test_the_chat_turn_sends_the_engines_grammar(_serve, fake_llama, monkeypatch):  # noqa: F811
    monkeypatch.setenv("H3_ENGINE", "sglang")
    srv = _serve(providers_port=fake_llama.port)
    sid = srv.post_json("/api/chat", {"source": {"kind": "new"}, "prompt": ""})["id"]
    srv.post_json(f"/api/chat/{sid}/message", {"text": "ролик", "prompt": ""})
    body = fake_llama.requests[-1]["body"]
    assert _scene_duration(body["response_format"]["json_schema"]) == {
        "type": "number", "minimum": 3.0, "maximum": 15.0}
