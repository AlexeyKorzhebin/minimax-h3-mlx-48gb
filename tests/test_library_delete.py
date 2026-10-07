"""Wave 1.5, spec §5.3: the library lists every version of a card, and a card is deleted only when
no project pins it -- moved to library/.trash, never erased."""
from pathlib import Path

import pytest

from h3_48gb import library as lib
from h3_48gb import project as p
from h3_48gb import queue as q
from test_web import _call, _serve

PNG = b"\x89PNG\r\n\x1a\n"


@pytest.fixture
def outdir(tmp_path):
    out = tmp_path / "outdir"
    (out / "uploads").mkdir(parents=True)
    (out / "uploads" / "a.png").write_bytes(PNG)
    lib.create_card(out, tag="@alice", kind="person", description="a woman",
                    assets=[out / "uploads" / "a.png"], now="2026-10-07T10:00:00")
    lib.update_card(out, "@alice", description="a woman in green", now="2026-10-07T11:00:00")
    return out


@pytest.fixture
def live(outdir, monkeypatch):
    monkeypatch.setenv("H3_ENGINE", "sglang")
    server = _serve(q.layout(outdir / "queue")["root"], outdir)
    yield server
    server.httpd.shutdown()
    server.httpd.server_close()


def test_the_listing_carries_every_version(live, outdir):
    status, body = _call(live, "GET", "/api/library")
    v1 = str(outdir / "library" / "alice" / "v1" / "01-a.png")
    assert status == 200
    assert body["cards"][0]["versions"] == [
        {"version": 1, "kind": "person", "description": "a woman", "assets": [v1],
         "created": "2026-10-07T10:00:00"},
        {"version": 2, "kind": "person", "description": "a woman in green", "assets": [v1],
         "created": "2026-10-07T11:00:00"}]


def test_delete_moves_the_card_to_the_trash(outdir):
    card_json = (outdir / "library" / "alice" / "card.json").read_text(encoding="utf-8")
    trashed = lib.delete_card(outdir, "@alice", pinned_by=[], now="20261007120000")
    assert trashed == outdir / "library" / ".trash" / "alice-20261007120000"
    assert (trashed / "card.json").read_text(encoding="utf-8") == card_json
    assert lib.list_cards(outdir) == []


def test_the_route_deletes_a_free_card_and_the_tag_can_be_made_again(live, outdir):
    status, body = _call(live, "DELETE", "/api/library/alice")
    assert status == 200, body
    trashed = Path(body["trashed"])
    assert (trashed.parent, sorted(x.name for x in trashed.parent.iterdir())) == (
        outdir / "library" / ".trash", [trashed.name])
    assert _call(live, "GET", "/api/library")[1]["cards"] == []
    lib.create_card(outdir, tag="@alice", kind="person", description="again",
                    assets=[outdir / "uploads" / "a.png"])
    assert [c["version"] for c in lib.list_cards(outdir)] == [1]


def test_a_card_pinned_by_a_finished_project_is_not_deleted(live, outdir):
    proj = p.create_project(outdir, "video", "Бой")
    proj.set_references([{"tag": "@alice", "version": 1}])
    proj.set_stage_status("assembly", "done")
    status, body = _call(live, "DELETE", "/api/library/alice")
    assert (status, body["error"]) == (409, {
        "code": "library_card_in_use",
        "message": f"@alice подключена к проектам: «Бой» ({proj.id}) — отключите её там или "
                   "удалите проекты",
        "detail": {"tag": "@alice", "projects": [{"id": proj.id, "title": "Бой"}]}})
    assert [c["tag"] for c in lib.list_cards(outdir)] == ["@alice"]


def test_a_card_named_only_by_a_scene_is_not_deleted(live, outdir):
    proj = p.create_project(outdir, "video", "Бой")
    proj.replace_scenes([{"idx": 0, "prompt": "x", "refs": ["@alice"]}])
    status, body = _call(live, "DELETE", "/api/library/alice")
    assert (status, body["error"]["detail"]["projects"]) == (
        409, [{"id": proj.id, "title": "Бой"}])


def test_an_unknown_card_is_404(live):
    status, body = _call(live, "DELETE", "/api/library/nobody")
    assert (status, body["error"]["code"]) == (404, "library_card_not_found")
