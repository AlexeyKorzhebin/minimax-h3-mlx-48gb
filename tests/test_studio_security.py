"""The studio on the LAN without a login (ruling R26): Host allow-list against DNS rebinding,
same-origin guard on every changing request."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from h3_48gb.studio.app import allowed_hosts_from_env, create_app

HOSTS = ["alex-neuro", "192.168.100.50", "localhost", "127.0.0.1"]


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path / "out", allowed_hosts=HOSTS)


def _client(app, host: str) -> TestClient:
    return TestClient(app, base_url=f"http://{host}")


@pytest.mark.parametrize("host", ["alex-neuro:8766", "192.168.100.50:8766", "localhost:8766",
                                  "127.0.0.1:8766"])
def test_the_studio_answers_its_own_names(app, host):
    assert _client(app, host).get("/api/studio/health").status_code == 200


@pytest.mark.parametrize("host", ["evil.example", "evil.example:8766", "192.168.100.51:8766"])
def test_a_foreign_host_is_400_dns_rebinding(app, host):
    client = _client(app, host)
    r = client.get("/api/studio/projects")
    assert (r.status_code, r.text) == (400, "Invalid host header")
    r = client.post("/api/studio/projects", json={"title": "x", "frame_format": "16:9"})
    assert r.status_code == 400
    assert _client(app, "127.0.0.1:8766").get("/api/studio/projects").json() == []


CROSS = {"Origin": "http://evil.example"}


@pytest.mark.parametrize("method, path, kwargs", [
    ("POST", "/api/studio/projects", {"json": {"title": "x", "frame_format": "16:9"}}),
    ("PATCH", "/api/studio/projects/p_x", {"json": {"expected_revision": 1}}),
    ("PUT", "/api/studio/projects/p_x/scenes", {"json": {"expected_revision": 1, "scenes": []}}),
    ("POST", "/api/studio/assets", {"files": {"file": ("a.png", b"x")}}),
    ("DELETE", "/api/studio/projects/p_x", {}),
])
def test_a_changing_request_from_another_site_is_403(app, method, path, kwargs):
    r = _client(app, "alex-neuro:8766").request(method, path, headers=CROSS, **kwargs)
    assert r.status_code == 403
    assert r.json() == {"error": {"code": "cross_origin",
                                  "message": "Запрос с чужого сайта отклонён",
                                  "detail": {"origin": "http://evil.example",
                                             "sec_fetch_site": None}}}


def test_sec_fetch_site_cross_site_without_origin_is_403(app):
    r = _client(app, "alex-neuro:8766").post(
        "/api/studio/projects", json={"title": "x", "frame_format": "16:9"},
        headers={"Sec-Fetch-Site": "cross-site"})
    assert (r.status_code, r.json()["error"]["code"]) == (403, "cross_origin")
    assert _client(app, "alex-neuro:8766").get("/api/studio/projects").json() == []


@pytest.mark.parametrize("headers", [
    {},                                                           # curl: no Origin at all
    {"Origin": "http://alex-neuro:8766", "Sec-Fetch-Site": "same-origin"},
    {"Origin": "http://alex-neuro:8766"},
    {"Sec-Fetch-Site": "none"},
])
def test_same_origin_and_no_origin_are_allowed(app, headers):
    r = _client(app, "alex-neuro:8766").post(
        "/api/studio/projects", json={"title": "x", "frame_format": "16:9"}, headers=headers)
    assert r.status_code == 201


def test_an_origin_with_another_port_is_another_site(app):
    r = _client(app, "alex-neuro:8766").post(
        "/api/studio/projects", json={"title": "x", "frame_format": "16:9"},
        headers={"Origin": "http://alex-neuro:8765"})       # the old panel's page
    assert r.status_code == 403


def test_reading_is_not_guarded(app):
    r = _client(app, "alex-neuro:8766").get("/api/studio/projects", headers=CROSS)
    assert r.status_code == 200


def test_allowed_hosts_come_from_the_defaults_and_the_environment():
    assert allowed_hosts_from_env({}) == ["alex-neuro", "localhost", "127.0.0.1"]
    assert allowed_hosts_from_env({"STUDIO_ALLOWED_HOSTS": "192.168.100.50, alex-neuro"}) == [
        "alex-neuro", "localhost", "127.0.0.1", "192.168.100.50"]


def _post_with(app, headers):
    return _client(app, "alex-neuro:8766").post(
        "/api/studio/projects", json={"title": "x", "frame_format": "16:9"}, headers=headers)


@pytest.mark.parametrize("origin", [
    "https://alex-neuro:8766",       # same host and port, another scheme (review r3)
    "//alex-neuro:8766",             # relative: no scheme at all
    "null",                          # sandboxed iframe, file://
    "http://[",                      # malformed: used to raise ValueError -> 500
    "http://alex-neuro:99999",       # port out of range
    "http://alex-neuro:8766/path",   # not a serialized origin
    "http://user@alex-neuro:8766",   # credentials
    "ftp://alex-neuro:8766",
    "ftp://alex-neuro",              # unknown scheme without a port: no default port to look up
    "http://alex-neuro:0",           # an explicit port 0 is not the default port (review r4)
])
def test_a_bad_or_foreign_origin_is_403_never_500(app, origin):
    r = _post_with(app, {"Origin": origin})
    assert r.status_code == 403
    assert r.json()["error"] == {"code": "cross_origin", "message": "Запрос с чужого сайта отклонён",
                                 "detail": {"origin": origin, "sec_fetch_site": None}}


@pytest.mark.parametrize("fetch_site", ["same-site", "cross-site", "anything"])
def test_only_same_origin_and_none_pass_sec_fetch_site(app, fetch_site):
    r = _post_with(app, {"Sec-Fetch-Site": fetch_site, "Origin": "http://alex-neuro:8766"})
    assert (r.status_code, r.json()["error"]["detail"]["sec_fetch_site"]) == (403, fetch_site)


def test_the_default_port_is_part_of_the_origin(app):
    """http://localhost and http://localhost:80 are one origin; :8766 is another."""
    client = _client(app, "localhost")
    ok = client.post("/api/studio/projects", json={"title": "x", "frame_format": "16:9"},
                     headers={"Origin": "http://localhost:80"})
    no = client.post("/api/studio/projects", json={"title": "x", "frame_format": "16:9"},
                     headers={"Origin": "http://localhost:8766"})
    zero = client.post("/api/studio/projects", json={"title": "x", "frame_format": "16:9"},
                       headers={"Origin": "http://localhost:0"})      # port 0 is not port 80
    assert (ok.status_code, no.status_code, zero.status_code) == (201, 403, 403)


# --- fix round 1 -----------------------------------------------------------------------------

def test_default_hosts_apply_without_an_explicit_allow_list(tmp_path, monkeypatch):
    monkeypatch.delenv("STUDIO_ALLOWED_HOSTS", raising=False)
    app = create_app(tmp_path / "out")
    assert _client(app, "evil.example").get("/api/studio/health").status_code == 400
    assert _client(app, "alex-neuro:8766").get("/api/studio/health").status_code == 200


def test_middleware_order_is_host_then_guard_then_body_limit(app):
    big = b"x" * (27 * 1024 * 1024)
    # guard before the body limit: a foreign origin with a body over even the upload limit -> 403
    r = _client(app, "alex-neuro:8766").post("/api/studio/assets", content=big, headers=CROSS)
    assert (r.status_code, r.json()["error"]["code"]) == (403, "cross_origin")
    # host before the guard: foreign host and an origin that differs from it -> 400, not 403
    # (an Origin equal to the foreign Host would pass the guard and prove nothing)
    r = _client(app, "evil.example").post(
        "/api/studio/projects", json={"title": "x", "frame_format": "16:9"},
        headers={"Origin": "http://other.example"})
    assert (r.status_code, r.text) == (400, "Invalid host header")
    # host before the body limit
    r = _client(app, "evil.example").post("/api/studio/assets", content=big)
    assert (r.status_code, r.text) == (400, "Invalid host header")


def test_the_403_is_json(app):
    r = _post_with(app, CROSS)
    assert (r.status_code, r.headers["content-type"]) == (403, "application/json")


@pytest.mark.parametrize("origin", [
    "http://alex-neuro:8766?a=1",         # query
    "http://alex-neuro:8766#x",           # fragment
    "http://:pass@alex-neuro:8766",       # password only (empty user name)
    "http://user:pass@alex-neuro:8766",   # user and password
    "http://:8766",                       # empty host name
])
def test_more_malformed_origins_are_403(app, origin):
    r = _post_with(app, {"Origin": origin})
    assert (r.status_code, r.json()["error"]["detail"]["origin"]) == (403, origin)


def test_origin_scheme_and_host_compare_case_insensitively(app):
    r = _post_with(app, {"Origin": "HTTP://ALEX-NEURO:8766"})
    assert r.status_code == 201
