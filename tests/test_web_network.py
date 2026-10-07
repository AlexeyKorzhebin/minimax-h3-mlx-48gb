"""LAN access (spec §3.3.6): the server may bind beyond the loopback only together with a
non-empty `H3_ALLOWED_HOSTS`, and Host/Origin are checked against that list by exact equality
(case-insensitive, since browsers lowercase the authority anyway)."""
import pytest

from h3_48gb import queue as q
from h3_48gb import web
from h3_48gb.cli import CliError
from test_web import _call, _json, _serve

LAN = "192.168.100.50:8765"


@pytest.fixture
def lan_server(tmp_path):
    outdir = tmp_path / "outdir"
    outdir.mkdir()
    root = q.layout(outdir / "queue")["root"]
    live = _serve(root, outdir, allowed_hosts=(LAN, "alex-neuro:8765"))
    yield live
    live.httpd.shutdown()
    live.httpd.server_close()


def test_a_lan_host_on_the_list_is_served(lan_server):
    status, body = _json(lan_server, "/api/state", host=LAN)
    assert status == 200, body
    assert body["ok"] is True


def test_a_write_from_the_lan_page_passes_both_checks(lan_server):
    status, body = _call(lan_server, "POST", "/api/queue/pause", {},
                         headers={"Host": LAN, "Origin": f"http://{LAN}"})
    assert (status, body) == (200, {"ok": True, "paused": True})


def test_host_comparison_ignores_case(lan_server):
    status, body = _json(lan_server, "/api/state", host="Alex-Neuro:8765")
    assert status == 200, body


@pytest.mark.parametrize("host", ["192.168.100.51:8765", "192.168.100.50:8766", "evil.example:8765"])
def test_a_host_off_the_list_is_refused(lan_server, host):
    status, body = _json(lan_server, "/api/state", host=host)
    assert status == 403
    assert body["error"]["code"] == "host_not_allowed"


def test_a_foreign_origin_is_refused_even_with_a_good_host(lan_server):
    status, body = _call(lan_server, "POST", "/api/queue/pause", {},
                         headers={"Host": LAN, "Origin": "http://evil.example:8765"})
    assert status == 403
    assert body["error"]["code"] == "origin_not_allowed"


def test_without_a_list_a_lan_host_is_still_refused(tmp_path):
    outdir = tmp_path / "outdir"
    outdir.mkdir()
    live = _serve(q.layout(outdir / "queue")["root"], outdir)
    try:
        status, body = _json(live, "/api/state", host=LAN)
        assert (status, body["error"]["code"]) == (403, "host_not_allowed")
    finally:
        live.httpd.shutdown()
        live.httpd.server_close()


def test_external_bind_without_a_list_is_refused_before_binding(tmp_path):
    with pytest.raises(CliError) as excinfo:
        web.make_server(tmp_path / "queue", tmp_path, port=0, host="0.0.0.0")
    assert excinfo.value.code == "external_bind_without_allowed_hosts"


def test_allowed_hosts_from_env_parses_and_validates():
    assert web.allowed_hosts_from_env({"H3_ALLOWED_HOSTS": " 192.168.100.50:8765, Alex-Neuro:8765 ,"}) \
        == ("192.168.100.50:8765", "alex-neuro:8765")
    assert web.allowed_hosts_from_env({}) == ()
    with pytest.raises(CliError) as excinfo:
        web.allowed_hosts_from_env({"H3_ALLOWED_HOSTS": "192.168.100.50"})
    assert excinfo.value.code == "allowed_hosts_invalid"


def test_cli_web_accepts_host_and_passes_env_list(monkeypatch, tmp_path):
    from h3_48gb import cli

    seen = {}

    class _FakeServer:
        server_address = ("0.0.0.0", 8765)

        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            pass

    def fake_make_server(root, outdir, **kwargs):
        seen.update(kwargs)
        return _FakeServer()

    monkeypatch.setattr(web, "make_server", fake_make_server)
    monkeypatch.setenv("H3_ALLOWED_HOSTS", LAN)
    (tmp_path / "out").mkdir()
    args = cli.build_parser().parse_args(["web", "--host", "0.0.0.0", "--outdir", str(tmp_path / "out")])
    assert args.host == "0.0.0.0"
    cli.run_web(args.outdir, args.port, args.host)
    assert seen == {"port": 8765, "verbose": True, "host": "0.0.0.0", "allowed_hosts": (LAN,)}
