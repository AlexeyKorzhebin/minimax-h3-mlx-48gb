"""Library and project-references JSON API (spec §3.5, §10: wave 1 is data + API)."""
import pytest

from h3_48gb import project as p
from h3_48gb import queue as q
from test_web import _call, _serve


@pytest.fixture
def live(tmp_path):
    outdir = tmp_path / "outdir"
    (outdir / "uploads").mkdir(parents=True)
    (outdir / "uploads" / "face.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    server = _serve(q.layout(outdir / "queue")["root"], outdir)
    yield server
    server.httpd.shutdown()
    server.httpd.server_close()


def _create(live, tag="@alice"):
    return _call(live, "POST", "/api/library", {
        "tag": tag, "kind": "person", "description": "a young woman",
        "assets": [str(live.outdir / "uploads" / "face.png")]})


def test_create_and_list(live):
    status, body = _create(live)
    expected_card = {"tag": "@alice", "kind": "person", "description": "a young woman",
                     "version": 1, "latest_version": 1,
                     "assets": [str(live.outdir / "library" / "alice" / "v1" / "01-face.png")]}
    assert (status, body) == (200, {"ok": True, "card": expected_card})
    assert _call(live, "GET", "/api/library") == (200, {"ok": True, "cards": [expected_card]})


def test_assets_outside_the_outdir_are_refused(live):
    status, body = _call(live, "POST", "/api/library", {
        "tag": "@alice", "kind": "person", "description": "d", "assets": ["/etc/hosts"]})
    assert (status, body["error"]["code"]) == (400, "path_outside_root")


def test_duplicate_tag_is_409(live):
    _create(live)
    status, body = _create(live)
    assert (status, body["error"]["code"]) == (409, "library_tag_exists")


def test_put_makes_version_two(live):
    _create(live)
    status, body = _call(live, "PUT", "/api/library/alice", {"description": "older woman"})
    assert status == 200
    assert (body["card"]["version"], body["card"]["description"]) == (2, "older woman")


def test_unknown_card_is_404(live):
    status, body = _call(live, "PUT", "/api/library/nobody", {"description": "x"})
    assert (status, body["error"]["code"]) == (404, "library_card_not_found")
    assert not (live.outdir / "library" / "nobody").exists()


def test_project_references_pin_latest_when_version_omitted(live):
    _create(live)
    _call(live, "PUT", "/api/library/alice", {"description": "v2"})
    proj = p.create_project(live.outdir, "video", "T")
    status, body = _call(live, "PUT", f"/api/projects/{proj.id}/references",
                         {"references": [{"tag": "@alice"}]})
    assert status == 200, body
    assert body["references"] == [{
        "tag": "@alice", "kind": "person", "description": "v2", "version": 2,
        "latest_version": 2,
        "assets": [str(live.outdir / "library" / "alice" / "v1" / "01-face.png")]}]
    assert p.load_project(proj.path).references == [{"tag": "@alice", "version": 2}]


def test_project_references_refuse_a_missing_version(live):
    _create(live)
    proj = p.create_project(live.outdir, "video", "T")
    status, body = _call(live, "PUT", f"/api/projects/{proj.id}/references",
                         {"references": [{"tag": "@alice", "version": 9}]})
    assert (status, body["error"]["code"]) == (400, "library_version_not_found")
    assert p.load_project(proj.path).references == []


def test_get_project_references(live):
    _create(live)
    proj = p.create_project(live.outdir, "video", "T")
    proj.set_references([{"tag": "@alice", "version": 1}])
    status, body = _call(live, "GET", f"/api/projects/{proj.id}/references")
    assert status == 200
    assert [ref["tag"] for ref in body["references"]] == ["@alice"]


@pytest.mark.parametrize("version", ["abc", [1], 1.9, True])
def test_a_non_integer_version_is_args_invalid(live, version):
    _create(live)
    proj = p.create_project(live.outdir, "video", "T")
    status, body = _call(live, "PUT", f"/api/projects/{proj.id}/references",
                         {"references": [{"tag": "@alice", "version": version}]})
    assert (status, body["error"]["code"], body["error"]["message"]) == (
        400, "args_invalid", "`version` must be an integer or omitted")
    assert p.load_project(proj.path).references == []


@pytest.mark.parametrize("versions", [(1, 1), (1, 2)])
def test_a_tag_pinned_twice_is_args_invalid(live, versions):
    _create(live)
    _call(live, "PUT", "/api/library/alice", {"description": "v2"})
    proj = p.create_project(live.outdir, "video", "T")
    status, body = _call(live, "PUT", f"/api/projects/{proj.id}/references", {
        "references": [{"tag": "@alice", "version": v} for v in versions]})
    assert (status, body["error"]["code"], body["error"]["detail"]) == (
        400, "args_invalid", {"tags": ["@alice"]})
    assert p.load_project(proj.path).references == []


def test_get_references_serves_the_pinned_version_not_the_latest(live):
    _create(live)
    proj = p.create_project(live.outdir, "video", "T")
    _call(live, "PUT", f"/api/projects/{proj.id}/references",
          {"references": [{"tag": "@alice", "version": 1}]})
    _call(live, "PUT", "/api/library/alice", {"description": "v2"})
    status, body = _call(live, "GET", f"/api/projects/{proj.id}/references")
    assert (status, body) == (200, {"ok": True, "references": [{
        "tag": "@alice", "kind": "person", "description": "a young woman", "version": 1,
        "latest_version": 2,
        "assets": [str(live.outdir / "library" / "alice" / "v1" / "01-face.png")]}]})
