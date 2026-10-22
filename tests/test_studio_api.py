"""Studio API, slice S0 (spec §7 O1–O4, O21; И10)."""
from __future__ import annotations

import time

from _studio_fixtures import LiveServer, make_client, read_sse

from h3_48gb.studio.app import create_app


def test_create_then_list_then_snapshot(tmp_path):
    client = make_client(tmp_path)
    r = client.post("/api/studio/projects",
                    json={"title": "Лес", "frame_format": "16:9", "idea_text": "девушка у озера"})
    assert r.status_code == 201
    created = r.json()
    assert created == {"id": created["id"], "revision": 1, "title": "Лес", "frame_format": "16:9",
                       "idea_text": "девушка у озера", "style_block": "",
                       "created_at": created["created_at"]}
    assert client.get("/api/studio/projects").json() == [
        {**created, "scenes_total": 0, "scenes_done": 0}]
    assert client.get(f"/api/studio/projects/{created['id']}").json() == {
        "project": created, "scenes": [], "cast": [], "takes": [], "tasks": [], "last_seq": 1}


def test_projects_survive_an_app_restart(tmp_path):
    first = make_client(tmp_path)
    pid = first.post("/api/studio/projects", json={"title": "Лес", "frame_format": "9:16"}).json()["id"]
    second = make_client(tmp_path)
    assert [p["id"] for p in second.get("/api/studio/projects").json()] == [pid]


def test_validation_error_is_the_envelope_with_400(tmp_path):
    r = make_client(tmp_path).post("/api/studio/projects",
                                   json={"title": "Лес", "frame_format": "4:3"})
    assert r.status_code == 400
    body = r.json()
    assert body["error"]["code"] == "args_invalid"
    assert body["error"]["message"] == "Неверные данные запроса"
    assert [e["loc"] for e in body["error"]["detail"]["errors"]] == [["body", "frame_format"]]


def test_missing_project_is_404_before_the_body_is_read(tmp_path):
    r = make_client(tmp_path).patch("/api/studio/projects/p_nope", json={"garbage": True})
    assert r.status_code == 404
    assert r.json() == {"error": {"code": "project_not_found", "message": "Проект не найден",
                                  "detail": {"project_id": "p_nope"}}}


def test_patch_with_a_stale_revision_is_409_with_the_current_project(tmp_path):
    client = make_client(tmp_path)
    p = client.post("/api/studio/projects", json={"title": "Лес", "frame_format": "16:9"}).json()
    ok = client.patch(f"/api/studio/projects/{p['id']}", json={"expected_revision": 1,
                                                               "title": "Лес-2"})
    assert (ok.status_code, ok.json()["revision"], ok.json()["title"]) == (200, 2, "Лес-2")
    stale = client.patch(f"/api/studio/projects/{p['id']}", json={"expected_revision": 1,
                                                                  "title": "Лес-3"})
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "revision_conflict"
    assert stale.json()["error"]["detail"]["current"] == ok.json()


def test_frame_format_is_locked_after_a_done_take(tmp_path):
    client = make_client(tmp_path)
    p = client.post("/api/studio/projects", json={"title": "Лес", "frame_format": "16:9"}).json()
    import sqlite3
    conn = sqlite3.connect(tmp_path / "out" / "studio" / "studio.sqlite")
    conn.execute("INSERT INTO scene (id, project_id, idx, mode, requested_s, frames, seed, "
                 "created_at) VALUES ('s', ?, 0, 'refs', 8.0, 192, 1, 'n')", (p["id"],))
    conn.execute("INSERT INTO take (id, scene_id, scene_revision, tier, executor, template, "
                 "status, created_at) VALUES ('t', 's', 1, 'sketch', 'comfy', 'h3-fl2va', 'done', "
                 "'n')")
    conn.commit()
    r = client.patch(f"/api/studio/projects/{p['id']}", json={"expected_revision": 1,
                                                              "frame_format": "9:16"})
    assert r.status_code == 409
    assert r.json()["error"] == {"code": "format_locked",
                                 "message": "Формат кадра нельзя менять после первого готового дубля",
                                 "detail": {"done_takes": 1}}


def test_snapshot_last_seq_resumes_the_stream_exactly(tmp_path):
    """И10: subscribing with Last-Event-ID = last_seq yields the next event and nothing older."""
    app = create_app(tmp_path / "out")
    with LiveServer(app) as live:
        from fastapi.testclient import TestClient
        client = TestClient(app, base_url="http://127.0.0.1:8766")
        p = client.post("/api/studio/projects", json={"title": "Лес", "frame_format": "16:9"}).json()
        client.patch(f"/api/studio/projects/{p['id']}", json={"expected_revision": 1, "title": "Б"})
        last = client.get(f"/api/studio/projects/{p['id']}").json()["last_seq"]
        assert last == 2
        replay = read_sse(live.port, "/api/studio/events", headers={"Last-Event-ID": "1"},
                          count=1, timeout=2)
        assert [(e["id"], e["event"], e["data"]["payload"]) for e in replay] == [
            (2, "project_updated", {"title": "Б"})]
        assert read_sse(live.port, "/api/studio/events", headers={"Last-Event-ID": str(last)},
                        count=1, timeout=1) == []
        client.post("/api/studio/projects", json={"title": "В", "frame_format": "9:16"})
        nxt = read_sse(live.port, f"/api/studio/events?after={last}", count=1, timeout=2)
        assert [(e["id"], e["event"]) for e in nxt] == [(3, "project_created")]


def test_master_is_served_from_the_built_dist(tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>Студия</title>")
    (dist / "assets" / "app.js").write_text("console.log(1)")
    client = make_client(tmp_path, web_dist=dist)
    assert client.get("/studio/").text == "<!doctype html><title>Студия</title>"
    assert client.get("/studio/p/p_123").text == "<!doctype html><title>Студия</title>"
    assert client.get("/studio/assets/app.js").text == "console.log(1)"
    assert client.get("/studio/..%2F..%2Fetc%2Fpasswd").text == "<!doctype html><title>Студия</title>"


def test_master_not_built_is_a_named_503(tmp_path):
    r = make_client(tmp_path, web_dist=tmp_path / "nothing").get("/studio/")
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "web_not_built"


def test_a_body_over_the_limit_is_413_before_anything_is_written(tmp_path):
    """Junk guard: a size limit on the request, not a cap on scenes (§4.4)."""
    client = make_client(tmp_path)
    junk = b'{"title": "' + b"x" * (2 * 1024 * 1024) + b'", "frame_format": "16:9"}'
    r = client.post("/api/studio/projects", content=junk,
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 413
    assert r.json() == {"error": {"code": "body_too_large", "message": "Запрос больше 2 МБ",
                                  "detail": {"max_bytes": 2 * 1024 * 1024}}}
    assert client.get("/api/studio/projects").json() == []


def test_a_chunked_body_over_the_limit_is_413_too(tmp_path):
    client = make_client(tmp_path)

    def chunks():
        yield b'{"title": "'
        for _ in range(3):
            yield b"x" * (1024 * 1024)
        yield b'", "frame_format": "16:9"}'

    r = client.post("/api/studio/projects", content=chunks(),
                    headers={"Content-Type": "application/json"})
    assert (r.status_code, r.json()["error"]["code"]) == (413, "body_too_large")


def test_snapshot_and_its_last_seq_come_from_one_read(tmp_path, monkeypatch):
    """И10 under a concurrent write: a change committed after the project row was read must not
    make `last_seq` claim its event — the client would then never see that change."""
    from h3_48gb.studio import db, events, ops
    client = make_client(tmp_path)
    p = client.post("/api/studio/projects", json={"title": "Лес", "frame_format": "16:9"}).json()
    real_last_seq = events.last_seq

    def write_in_between(conn):
        monkeypatch.setattr(events, "last_seq", real_last_seq)
        other = db.connect(tmp_path / "out" / "studio" / "studio.sqlite")
        try:
            ops.update_project(other, p["id"], expected_revision=1, title="Б")
        finally:
            other.close()
        return real_last_seq(conn)

    monkeypatch.setattr(events, "last_seq", write_in_between)
    snap = client.get(f"/api/studio/projects/{p['id']}").json()
    assert (snap["project"]["title"], snap["project"]["revision"], snap["last_seq"]) == ("Лес", 1, 1)
    snap = client.get(f"/api/studio/projects/{p['id']}").json()
    assert (snap["project"]["title"], snap["project"]["revision"], snap["last_seq"]) == ("Б", 2, 2)


def test_an_sse_read_in_flight_keeps_its_connection_until_it_returns(tmp_path, monkeypatch):
    """С-1, deterministically: the client goes away while a journal read is running in a worker
    thread. The connection that read uses may close only after the read has returned — closing
    it under a running query is what crashed sqlite3 in the image."""
    import socket
    import threading

    from h3_48gb.studio import db as studio_db
    from h3_48gb.studio import events

    app = create_app(tmp_path / "out")
    log: list[str] = []
    started, release = threading.Event(), threading.Event()
    real_connect = studio_db.connect

    class Tracked:
        def __init__(self, conn):
            self._conn = conn

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def close(self):
            log.append("close")
            self._conn.close()

    def slow_read(conn, after, limit=100):
        log.append("read_start")
        started.set()
        release.wait(5)
        log.append("read_end")
        return []

    monkeypatch.setattr(studio_db, "connect", lambda path: Tracked(real_connect(path)))
    monkeypatch.setattr(events, "read_after", slow_read)
    with LiveServer(app) as live:
        sock = socket.create_connection(("127.0.0.1", live.port))
        sock.sendall(b"GET /api/studio/events HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        assert started.wait(5)
        sock.close()                              # the tab goes away in the middle of the read
        time.sleep(0.5)                           # the server notices and cancels the stream
        assert log == ["read_start"]              # nothing closed under the running read
        release.set()
        deadline = time.monotonic() + 5
        while "close" not in log and time.monotonic() < deadline:
            time.sleep(0.02)
    assert log[:3] == ["read_start", "read_end", "close"]


# --- content of responses, not just status (added in task 3) ---------------------------------

def test_health_body_is_exact(tmp_path):
    r = make_client(tmp_path).get("/api/studio/health")
    assert (r.status_code, r.json()) == (200, {"ok": True, "schema_version": 1})


def test_root_redirects_to_the_master(tmp_path):
    r = make_client(tmp_path).get("/", follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (307, "/studio/")


def test_unknown_api_route_is_the_not_found_envelope(tmp_path):
    r = make_client(tmp_path).get("/api/studio/nothing-here")
    assert (r.status_code, r.json()) == (404, {"error": {"code": "not_found",
                                                          "message": "Not Found", "detail": {}}})


def test_extra_and_empty_fields_are_args_invalid_with_their_location(tmp_path):
    client = make_client(tmp_path)
    r = client.post("/api/studio/projects",
                    json={"title": "Лес", "frame_format": "16:9", "surprise": 1})
    assert (r.status_code, [e["loc"] for e in r.json()["error"]["detail"]["errors"]]) == (
        400, [["body", "surprise"]])
    r = client.post("/api/studio/projects", json={"title": "", "frame_format": "16:9"})
    assert (r.status_code, [e["loc"] for e in r.json()["error"]["detail"]["errors"]]) == (
        400, [["body", "title"]])
    assert client.get("/api/studio/projects").json() == []


def test_patch_with_a_bad_frame_format_is_400_args_invalid(tmp_path):
    client = make_client(tmp_path)
    p = client.post("/api/studio/projects", json={"title": "Лес", "frame_format": "16:9"}).json()
    r = client.patch(f"/api/studio/projects/{p['id']}",
                     json={"expected_revision": 1, "frame_format": "4:3"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "args_invalid"
    assert [e["loc"] for e in r.json()["error"]["detail"]["errors"]] == [["body", "frame_format"]]
    assert client.get(f"/api/studio/projects/{p['id']}").json()["project"] == p


def test_the_event_stream_headers_and_frame_bytes(tmp_path):
    """What the browser's EventSource gets on the wire: type, no caching, the exact frame."""
    import http.client
    import json

    from fastapi.testclient import TestClient
    app = create_app(tmp_path / "out")
    with LiveServer(app) as live:
        client = TestClient(app, base_url="http://127.0.0.1:8766")
        client.post("/api/studio/projects", json={"title": "Лес", "frame_format": "16:9"})
        conn = http.client.HTTPConnection("127.0.0.1", live.port, timeout=3)
        conn.request("GET", "/api/studio/events")
        resp = conn.getresponse()
        assert resp.status == 200
        assert resp.getheader("Content-Type") == "text/event-stream; charset=utf-8"
        assert resp.getheader("Cache-Control") == "no-store"
        assert resp.getheader("X-Accel-Buffering") == "no"
        lines = [resp.readline().decode() for _ in range(4)]
        conn.close()
    assert lines[:2] == ["id: 1\n", "event: project_created\n"]
    event = json.loads(lines[2][len("data: "):])
    assert (event["seq"], event["kind"], event["payload"]) == (
        1, "project_created", {"frame_format": "16:9", "title": "Лес"})
    assert lines[3] == "\n"


def test_the_upload_path_has_its_own_larger_limit(tmp_path):
    """3 MiB is over the default limit but under the upload limit: not a 413 on the assets path
    (the route itself comes in a later task — 404 now)."""
    client = make_client(tmp_path)
    assert client.post("/api/studio/assets", content=b"x" * (3 * 1024 * 1024)).status_code != 413
    over = client.post("/api/studio/assets", content=b"x" * (27 * 1024 * 1024))
    assert (over.status_code, over.json()["error"]["detail"]) == (
        413, {"max_bytes": 26 * 1024 * 1024})
    assert over.json()["error"]["message"] == "Запрос больше 26 МБ"


def test_the_master_never_serves_a_file_outside_dist(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("INDEX")
    (tmp_path / "secret.txt").write_text("SECRET")
    client = make_client(tmp_path, web_dist=dist)
    for path in ("/studio/..%2Fsecret.txt", "/studio/../secret.txt", "/studio/%2e%2e/secret.txt"):
        assert "SECRET" not in client.get(path).text, path
    assert client.get("/studio/..%2Fsecret.txt").text == "INDEX"
