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


def _edit_project_json(proj, **fields):
    import json
    path = proj.path
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(fields)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _in_use(live, proj):
    status, body = _call(live, "DELETE", "/api/library/alice")
    assert (status, body["error"]["code"], body["error"]["detail"]["projects"]) == (
        409, "library_card_in_use", [{"id": proj.id, "title": "Бой"}])


def test_a_card_used_as_a_scenes_start_image_is_not_deleted(live, outdir):
    proj = p.create_project(outdir, "video", "Бой")
    proj.replace_scenes([{"idx": 0, "prompt": "x", "start_image": "@alice"}])
    _in_use(live, proj)


def test_a_card_used_as_the_projects_start_image_is_not_deleted(live, outdir):
    proj = p.create_project(outdir, "video", "Бой")
    _edit_project_json(proj, start_image="@alice")
    _in_use(live, proj)


def test_a_project_the_listing_skips_still_blocks_when_it_names_the_tag(live, outdir):
    proj = p.create_project(outdir, "video", "Бой")
    _edit_project_json(proj, scenes="not-a-list", references=[{"tag": "@alice", "version": 1}])
    with pytest.warns(UserWarning, match="skipping corrupt project"):
        assert proj.id not in [x.id for x in p.list_projects(outdir)]
    _in_use(live, proj)


def test_an_unreadable_project_refuses_the_delete_with_a_reason(live, outdir):
    proj = p.create_project(outdir, "video", "Бой")
    proj.path.write_text("{broken", encoding="utf-8")
    status, body = _call(live, "DELETE", "/api/library/alice")
    assert (status, body["error"]["code"], body["error"]["detail"]["projects"]) == (
        409, "library_card_in_use", [{"id": proj.id, "title": "project.json не читается"}])


def test_delete_create_delete_within_one_second_keeps_both_trashed_copies(outdir):
    first = lib.delete_card(outdir, "@alice", pinned_by=[], now="20261007120000")
    lib.create_card(outdir, tag="@alice", kind="person", description="again",
                    assets=[outdir / "uploads" / "a.png"])
    second = lib.delete_card(outdir, "@alice", pinned_by=[], now="20261007120000")
    assert (first.name, second.name) == ("alice-20261007120000", "alice-20261007120000-2")
    assert [lib.json.loads((d / "card.json").read_text(encoding="utf-8"))["version"]
            for d in (first, second)] == [2, 1]


def test_the_trash_keeps_every_version_file_of_the_card(outdir):
    trashed = lib.delete_card(outdir, "@alice", pinned_by=[], now="20261007120000")
    assert (trashed / "v1" / "01-a.png").read_bytes() == PNG


def test_pinned_by_is_decided_under_the_card_lock(outdir):
    seen = []

    def pinned():
        import fcntl
        with open(outdir / "library" / "alice" / "card.lock", "a+") as other:
            try:
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
                seen.append("free")
            except BlockingIOError:
                seen.append("held")
        return []

    lib.delete_card(outdir, "@alice", pinned_by=pinned, now="20261007120000")
    assert seen == ["held"]


def test_a_writer_waiting_on_a_deleted_card_does_not_resurrect_its_directory(outdir):
    import threading
    card_dir = outdir / "library" / "alice"
    outcome = []

    def writer():
        try:
            lib.update_card(outdir, "@alice", description="late")
        except lib.LibraryError as exc:
            outcome.append(exc.code)

    with lib._card_lock(card_dir, "@alice"):
        thread = threading.Thread(target=writer)
        thread.start()
        import time
        time.sleep(0.3)
        card_dir.rename(outdir / "library" / ".trash-moved")
    thread.join(5)
    assert (outcome, card_dir.exists()) == (["library_card_not_found"], False)


def test_locking_a_card_that_is_gone_does_not_recreate_its_directory(outdir):
    gone = outdir / "library" / "bob"
    with pytest.raises(lib.LibraryError) as err:
        with lib._card_lock(gone, "@bob"):
            pass
    assert (err.value.code, gone.exists()) == ("library_card_not_found", False)


def test_card_paths_and_the_state_outdir_agree_through_a_symlinked_outdir(tmp_path, monkeypatch):
    """The page builds `/media/...` for a card's picture only when the path starts with the
    `outdir` of `/api/state`. With `H3_OUTDIR` behind a symlink the state used to say the resolved
    path and the cards the unresolved one -- no thumbnail, no `<Picture k>` preview."""
    monkeypatch.setenv("H3_ENGINE", "sglang")
    real = tmp_path / "real"
    (real / "uploads").mkdir(parents=True)
    (real / "uploads" / "a.png").write_bytes(PNG)
    lib.create_card(real, tag="@alice", kind="person", description="a woman",
                    assets=[real / "uploads" / "a.png"])
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    server = _serve(q.layout(tmp_path / "queue")["root"], link)
    try:
        state_outdir = _call(server, "GET", "/api/state")[1]["outdir"]
        asset = _call(server, "GET", "/api/library")[1]["cards"][0]["assets"][0]
    finally:
        server.httpd.shutdown()
        server.httpd.server_close()
    assert asset.startswith(state_outdir + "/")
