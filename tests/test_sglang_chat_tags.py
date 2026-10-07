"""Chat tags ride the system message (spec §3.5)."""
from h3_48gb import library as lib
from test_chat_web import _serve, fake_llama  # noqa: F401  (fixtures)


def test_chat_tags_ride_the_system_message(_serve, fake_llama):  # noqa: F811
    srv = _serve(providers_port=fake_llama.port)
    (srv.root / "uploads").mkdir(exist_ok=True)
    (srv.root / "uploads" / "f.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    card = lib.create_card(srv.root, tag="@alice", kind="person", description="a young woman",
                           assets=[srv.root / "uploads" / "f.png"])
    sid = srv.post_json("/api/chat", {"source": {"kind": "new"}, "prompt": ""})["id"]
    srv.post_json(f"/api/chat/{sid}/message", {"text": "сцена", "prompt": "", "tags": ["@alice"]})
    system = fake_llama.requests[-1]["body"]["messages"][0]["content"]
    assert system.endswith("\n\n" + lib.references_context([card]))
    # the tags stay with the session: a later turn without `tags` still carries them
    srv.post_json(f"/api/chat/{sid}/message", {"text": "ещё", "prompt": ""})
    assert fake_llama.requests[-1]["body"]["messages"][0]["content"].endswith(
        "\n\n" + lib.references_context([card]))
