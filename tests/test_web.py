"""The server: path policy, the worker probe, the read routes, and the shape of every failure.

**Nothing here starts a real generation** and nothing here loads a weight. Every test either calls
a pure function or talks HTTP to a server bound to port 0 in this process.

Three shapes recur, and each exists because the obvious version of the test passes against the bug
it is meant to catch:

* **A refusal is checked by its code, never by "not 200".** A traversal test that accepts any 404
  passes on a server whose router simply failed to match the URL -- i.e. on a server with no path
  checking whatsoever. Every escape below asserts `400` *and* `path_outside_root`.
* **Every path check is exercised from both sides.** "Outside is refused" is satisfied by a
  function that refuses everything; the paired "inside is served" is what says the refusal is a
  policy rather than a wall.
* **The worker probe is answered by a lock held in a separate process.** `flock` is per-process, so
  a thread in this one re-acquires its own lock and proves nothing. `tests/test_queue.py` already
  has `_external_lock` for exactly this; it is imported rather than rewritten.
"""
import http.client
import json
import os
import subprocess
import sys
import threading
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

import pytest

from h3_48gb import queue as q
from h3_48gb import web
from h3_48gb.cli import DEFAULT_CHECKPOINT, CliError, build_parser
# `flock` is only honest when the holder is a separate process -- see the module docstring.
from test_queue import _DRY, _external_lock

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# -- helpers -----------------------------------------------------------------------------------


def _roots(tmp_path) -> dict[str, Path]:
    """Three roots under `tmp_path`, named exactly as `web.default_roots` names them -- the name
    `models` is what `READ_ONLY_ROOTS` matches on, so a test that renamed it would silently be
    testing a writable third root.
    """
    roots = {name: tmp_path / name for name in ("repo", "outdir", "models")}
    for path in roots.values():
        path.mkdir()
    return roots


def _generate_subparser(parser):
    """The `generate` subparser out of a built top-level parser. argparse offers no public way to
    reach one, and hunting for the private attribute in each test would repeat the same guess.
    """
    for action in parser._actions:
        if isinstance(getattr(action, "choices", None), dict) and "generate" in action.choices:
            return action.choices["generate"]
    raise AssertionError("build_parser() has no `generate` subcommand any more")


_PATH_FLAG_NAMES = sorted(web.PATH_FLAGS)


@dataclass
class _Live:
    """A running server plus the directories it was pointed at."""

    httpd: object
    port: int
    queue_root: Path
    outdir: Path
    webui: Path


def _serve(queue_root, outdir, **kwargs) -> _Live:
    httpd = web.make_server(queue_root, outdir, port=0, **kwargs)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return _Live(httpd=httpd, port=httpd.server_address[1], queue_root=Path(queue_root),
                 outdir=Path(outdir), webui=Path(httpd.webui))


@pytest.fixture
def server(tmp_path):
    """A server on the **real** repository and webui roots, with a temporary outdir and queue.

    Real roots on purpose: `/static/../cli.py` is only an interesting request when `cli.py` is
    really there to be served, which is the whole point of the static root being `webui/` rather
    than the repository (see `web.WEBUI_ROOT`). A temporary `webui` would make that URL escape into
    an empty tmp directory and the test would pass for the wrong reason.
    """
    outdir = tmp_path / "outdir"
    outdir.mkdir()
    root = q.layout(outdir / "queue")["root"]
    live = _serve(root, outdir)
    yield live
    live.httpd.shutdown()
    live.httpd.server_close()


def _request(live: _Live, url: str, method: str = "GET"):
    """`(status, headers, body_bytes)` for a raw URL, sent verbatim.

    `http.client` does not normalise the request target, which is what makes `/static/../cli.py`
    reach the server as written instead of being collapsed by the client.
    """
    connection = http.client.HTTPConnection(web.LOOPBACK, live.port, timeout=10)
    try:
        connection.request(method, url)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def _json(live: _Live, url: str, method: str = "GET"):
    status, headers, body = _request(live, url, method)
    assert headers["Content-Type"] == "application/json", (
        f"{url} answered {headers['Content-Type']!r}; the contract is JSON everywhere")
    return status, json.loads(body)


# -- Step 1: the path policy -------------------------------------------------------------------


@pytest.mark.parametrize("candidate", ["../../etc/passwd", "/etc/passwd",
                                       "prompts/../../secrets.txt"])
def test_paths_outside_every_root_are_refused(tmp_path, candidate):
    roots = _roots(tmp_path)
    # Relative candidates are anchored under a root, so what makes them escape is the `..` they
    # contain rather than the directory the test happened to start from.
    path = roots["repo"] / candidate if not candidate.startswith("/") else Path(candidate)
    with pytest.raises(CliError) as excinfo:
        web.resolve_within(path, roots, write=False)
    assert excinfo.value.code == "path_outside_root"


@pytest.mark.parametrize("name", ["repo", "outdir", "models"])
def test_a_path_inside_a_root_is_accepted(tmp_path, name):
    """The other half of the test above: a policy that refused everything would pass that one."""
    roots = _roots(tmp_path)
    wanted = roots[name] / "sub" / "file.txt"
    assert web.resolve_within(wanted, roots, write=False) == wanted.resolve()


def test_a_path_that_does_not_exist_yet_still_resolves(tmp_path):
    """`--outdir` names a directory the run is about to create. A strict `resolve()` would refuse
    the most ordinary request the form can make.
    """
    roots = _roots(tmp_path)
    wanted = roots["outdir"] / "20-новый-прогон"
    assert not wanted.exists()
    assert web.resolve_within(wanted, roots, write=True) == wanted.resolve()


def test_a_symlink_pointing_out_of_a_root_is_refused(tmp_path):
    """Text comparison is not enough: resolve() is what sees through a link."""
    roots = _roots(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("s")
    link = roots["repo"] / "innocent.txt"
    link.symlink_to(outside / "secret.txt")
    assert str(link).startswith(str(roots["repo"])), "the link's *text* is inside the root"
    with pytest.raises(CliError) as excinfo:
        web.resolve_within(link, roots, write=False)
    assert excinfo.value.code == "path_outside_root"


def test_a_symlink_that_stays_inside_a_root_is_accepted(tmp_path):
    """Paired with the test above: refusing every symlink would satisfy that one and break the
    ordinary case of a prompts directory that is itself a link.
    """
    roots = _roots(tmp_path)
    target = roots["repo"] / "real.txt"
    target.write_text("t")
    link = roots["repo"] / "link.txt"
    link.symlink_to(target)
    assert web.resolve_within(link, roots, write=False) == target.resolve()


def test_the_models_root_is_readable_but_never_writable(tmp_path):
    roots = _roots(tmp_path)
    weights = roots["models"] / "h3-converted" / "transformer"
    assert web.resolve_within(weights, roots, write=False) == weights.resolve()
    with pytest.raises(CliError) as excinfo:
        web.resolve_within(weights, roots, write=True)
    assert excinfo.value.code == "path_outside_root"


@pytest.mark.parametrize("name", ["repo", "outdir"])
def test_the_other_two_roots_stay_writable(tmp_path, name):
    """`write=True` must drop the models root and nothing else -- a `write` flag that refused
    every root would pass the test above.
    """
    roots = _roots(tmp_path)
    wanted = roots[name] / "out.mp4"
    assert web.resolve_within(wanted, roots, write=True) == wanted.resolve()


def test_the_clis_own_default_checkpoint_is_inside_a_root(tmp_path):
    """Why there are three roots and not two: `h3 generate`'s default `--checkpoint` is
    `~/models/h3-converted`, so a repo+outdir policy would refuse this CLI's own default value.
    """
    roots = web.default_roots(tmp_path)
    assert web.resolve_within(DEFAULT_CHECKPOINT, roots, write=False) == DEFAULT_CHECKPOINT.resolve()


def test_the_models_root_follows_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("H3_MODELS_ROOT", str(tmp_path / "elsewhere"))
    assert web.models_root() == tmp_path / "elsewhere"
    assert web.default_roots(tmp_path)["models"] == tmp_path / "elsewhere"


def test_check_path_flags_covers_every_flag_the_parser_knows_about():
    """A new path flag added to the CLI without a policy here is a hole. Compare against the
    parser rather than a hand-kept list.
    """
    gen = _generate_subparser(build_parser())
    path_flags = {a.option_strings[0] for a in gen._actions
                  if getattr(a, "type", None) is Path and a.option_strings}
    assert path_flags, "the scan found no path flags at all -- it is looking in the wrong place"
    assert path_flags <= set(web.PATH_FLAGS), (
        f"these CLI flags take a path but have no policy: {path_flags - set(web.PATH_FLAGS)}")


def test_the_policy_lists_no_flag_the_parser_does_not_have():
    """The other direction: a stale entry left behind by a renamed flag is a policy that guards
    nothing, and it makes the test above pass by widening the set it compares against.
    """
    gen = _generate_subparser(build_parser())
    known = {name for a in gen._actions for name in a.option_strings}
    assert set(web.PATH_FLAGS) <= known, (
        f"policy names flags `h3 generate` does not have: {set(web.PATH_FLAGS) - known}")


@pytest.mark.parametrize("flag,value_outside", [(f, "/etc") for f in _PATH_FLAG_NAMES])
def test_every_path_flag_is_checked(tmp_path, flag, value_outside):
    with pytest.raises(CliError) as excinfo:
        web.check_path_flags(["generate", "x", flag, value_outside], _roots(tmp_path))
    assert excinfo.value.code == "path_outside_root"


@pytest.mark.parametrize("flag", _PATH_FLAG_NAMES)
def test_every_path_flag_accepts_a_value_inside_a_root(tmp_path, flag):
    """Paired with the test above. A `check_path_flags` that raised on every flag it recognised
    would satisfy that one perfectly.
    """
    roots = _roots(tmp_path)
    web.check_path_flags(["generate", "x", flag, str(roots["outdir"] / "value")], roots)


@pytest.mark.parametrize("flag", _PATH_FLAG_NAMES)
def test_the_equals_spelling_is_checked_too(tmp_path, flag):
    """`--outdir=/etc` is the same request as `--outdir /etc` to argparse, so a checker that only
    understood the space-separated form would be a hole with a green suite in front of it.
    """
    with pytest.raises(CliError) as excinfo:
        web.check_path_flags(["generate", "x", f"{flag}=/etc"], _roots(tmp_path))
    assert excinfo.value.code == "path_outside_root"


@pytest.mark.parametrize("flag", [f for f, mode in web.PATH_FLAGS.items() if mode == "write"])
def test_a_write_flag_may_not_point_into_the_models_root(tmp_path, flag):
    roots = _roots(tmp_path)
    with pytest.raises(CliError) as excinfo:
        web.check_path_flags(["generate", "x", flag, str(roots["models"] / "out")], roots)
    assert excinfo.value.code == "path_outside_root"


@pytest.mark.parametrize("flag", [f for f, mode in web.PATH_FLAGS.items() if mode == "read"])
def test_a_read_flag_may_point_into_the_models_root(tmp_path, flag):
    """The weights are what the read flags are *for*: `--checkpoint`, `--turbo-lora` and
    `--adaln-cache` all live under `~/models` in every real invocation.
    """
    roots = _roots(tmp_path)
    web.check_path_flags(["generate", "x", flag, str(roots["models"] / "h3-converted")], roots)


def test_a_value_that_merely_looks_like_a_flag_is_not_treated_as_one(tmp_path):
    """`--tag` is not a path flag, so its value is none of this function's business -- including
    when that value happens to be `/etc`.
    """
    web.check_path_flags(["generate", "x", "--tag", "/etc"], _roots(tmp_path))


def test_a_path_flag_with_no_value_is_left_to_argparse(tmp_path):
    """Trailing `--outdir` with nothing after it must not crash here; the validation subprocess
    reports it with a better message than this function could invent.
    """
    web.check_path_flags(["generate", "x", "--outdir"], _roots(tmp_path))


# -- prompt names ------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["../secrets.txt", "sub/x.txt", "/etc/passwd.txt", "x.md",
                                  "", ".txt", "x.txt.md", "пробел .txt"])
def test_a_prompt_name_that_is_not_a_bare_filename_is_refused(name):
    with pytest.raises(CliError) as excinfo:
        web.resolve_prompt_name(name)
    assert excinfo.value.code == "prompt_name_invalid"


def test_a_plain_prompt_name_resolves_inside_the_repository(tmp_path):
    repo = tmp_path / "repo"
    (repo / "prompts").mkdir(parents=True)
    assert web.resolve_prompt_name("centaur-battle.txt", repo=repo) == \
        (repo / "prompts" / "centaur-battle.txt").resolve()


# -- Step 3: the worker probe ------------------------------------------------------------------


def test_worker_state_is_alive_while_another_process_holds_the_lock(tmp_path):
    root = q.layout(tmp_path / "queue")["root"]
    with _external_lock(root, "LOCK_EX", name=web.WORKER_LOCK_NAME):
        assert web.worker_state(root) == "alive"


def test_worker_state_is_stopped_once_the_lock_is_released(tmp_path):
    """The other half. A probe hardcoded to `alive` passes the test above; only watching the same
    queue go back to `stopped` tells the two apart.
    """
    root = q.layout(tmp_path / "queue")["root"]
    with _external_lock(root, "LOCK_EX", name=web.WORKER_LOCK_NAME):
        assert web.worker_state(root) == "alive"
    assert web.worker_state(root) == "stopped", "the lock file outlived its holder's grip on it"


def test_worker_state_is_stopped_when_no_lock_file_exists(tmp_path):
    root = q.layout(tmp_path / "queue")["root"]
    assert not (root / web.WORKER_LOCK_NAME).exists()
    assert web.worker_state(root) == "stopped"


def test_probing_the_worker_never_creates_the_lock_file(tmp_path):
    """`worker.hold_worker_lock` opens the file with `O_CREAT`, which is right for a worker and
    wrong for a question. A server that probed the same way would invent a `worker.lock` on the
    first `/api/state` and then answer questions about a file it made up.
    """
    root = q.layout(tmp_path / "queue")["root"]
    for _ in range(3):
        web.worker_state(root)
    assert not (root / web.WORKER_LOCK_NAME).exists(), "the probe created the lock file"


def test_worker_state_is_unknown_when_the_probe_itself_fails(tmp_path, monkeypatch):
    """Neither `alive` nor `stopped` may be guessed when `flock` could not answer: the page says
    "unknown" out loud rather than promising a human the queue is about to move.
    """
    root = q.layout(tmp_path / "queue")["root"]
    (root / web.WORKER_LOCK_NAME).write_bytes(b"")

    def refuse(*args, **kwargs):
        raise OSError(45, "Operation not supported")

    monkeypatch.setattr(web.fcntl, "flock", refuse)
    assert web.worker_state(root) == "unknown"


@pytest.mark.skipif(os.geteuid() == 0, reason="root opens a 000 file regardless of its mode")
def test_worker_state_is_unknown_when_the_lock_file_cannot_be_opened(tmp_path):
    root = q.layout(tmp_path / "queue")["root"]
    lock = root / web.WORKER_LOCK_NAME
    lock.write_bytes(b"")
    lock.chmod(0o000)
    try:
        assert web.worker_state(root) == "unknown"
    finally:
        lock.chmod(0o644)


# -- Step 4: /api/state ------------------------------------------------------------------------


def _submit(root, tag, outdir):
    return q.submit(root, ["generate", "--tag", tag], "", {**_DRY, "output_stem": str(outdir / tag)},
                    {})


def test_api_state_splits_the_queue_by_directory(server):
    pending = _submit(server.queue_root, "waiting", server.outdir)
    _submit(server.queue_root, "going", server.outdir)
    running = q.claim(server.queue_root)

    status, body = _json(server, "/api/state")
    assert status == 200 and body["ok"] is True
    assert [job["id"] for job in body["queue"]["pending"]] == [pending.id]
    assert [job["id"] for job in body["queue"]["running"]] == [running.id]
    assert body["queue"]["done"] == [] and body["queue"]["failed"] == []
    assert body["runs"] == []


def test_api_state_orders_pending_the_way_the_worker_will_claim_it(server):
    first = _submit(server.queue_root, "aaa", server.outdir)
    last = _submit(server.queue_root, "zzz", server.outdir)
    q.move_to_front(server.queue_root, last.id)

    _, body = _json(server, "/api/state")
    assert [job["id"] for job in body["queue"]["pending"]] == [last.id, first.id], (
        "the page's first job must be the job the worker takes next")


def test_api_state_reports_a_broken_job_file_instead_of_dropping_it(server):
    (server.queue_root / "pending" / "20260811-000000-bad-0000.json").write_text("{oops")
    _, body = _json(server, "/api/state")
    assert body["queue"]["pending"] == []
    assert len(body["queue"]["broken"]) == 1
    assert body["queue"]["broken"][0]["path"].endswith("20260811-000000-bad-0000.json")


def test_api_state_says_the_worker_is_stopped_when_none_is_running(server):
    _, body = _json(server, "/api/state")
    assert body["worker"]["state"] == "stopped"


def test_api_state_says_the_worker_is_alive_while_one_holds_the_lock(server):
    with _external_lock(server.queue_root, "LOCK_EX", name=web.WORKER_LOCK_NAME):
        _, body = _json(server, "/api/state")
        assert body["worker"]["state"] == "alive"
    _, body = _json(server, "/api/state")
    assert body["worker"]["state"] == "stopped", "the state never came back down"


def test_polling_api_state_does_not_create_the_worker_lock(server):
    for _ in range(3):
        _json(server, "/api/state")
    assert not (server.queue_root / web.WORKER_LOCK_NAME).exists()


def test_api_state_reports_a_run_in_flight(server, monkeypatch):
    """`runs.scan` is what fills the third block; the endpoint must actually call it rather than
    hand back an empty list that looks identical on an idle machine.
    """
    from h3_48gb.runs import Run

    monkeypatch.setattr(web.runs_module, "scan",
                        lambda root: [Run(outdir=Path(root) / "19", tag="t", state="in_flight",
                                          completed=3, total=7)])
    _, body = _json(server, "/api/state")
    assert [(run["state"], run["completed"], run["total"]) for run in body["runs"]] == \
        [("in_flight", 3, 7)]


def test_a_queue_that_cannot_be_read_is_a_500_naming_the_directory(server, monkeypatch):
    def unreadable(root):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(web.q, "scan", unreadable)
    status, body = _json(server, "/api/state")
    assert status == 500
    assert body["error"]["code"] == "queue_unwritable"
    assert body["error"]["detail"]["path"] == str(server.queue_root)


def test_an_unexpected_exception_becomes_json_with_its_type(server, monkeypatch):
    monkeypatch.setattr(web, "build_state",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    status, body = _json(server, "/api/state")
    assert status == 500
    assert body["error"]["code"] == "internal_error"
    assert body["error"]["detail"]["type"] == "RuntimeError"
    assert "boom" not in json.dumps(body), "the message can carry a prompt or a path; only the type"


# -- Step 5/6: static, media, and traversal ------------------------------------------------------


def test_the_index_page_is_served_at_the_root(server):
    status, headers, body = _request(server, "/")
    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    assert b"<html" in body.lower()


def test_a_static_file_comes_from_the_webui_directory(tmp_path):
    webui = tmp_path / "webui"
    webui.mkdir()
    (webui / "app.js").write_text("console.log(1)\n")
    outdir = tmp_path / "outdir"
    outdir.mkdir()
    live = _serve(outdir / "queue", outdir, webui=webui)
    try:
        status, headers, body = _request(live, "/static/app.js")
        assert status == 200
        assert headers["Content-Type"].startswith("text/javascript")
        assert body == b"console.log(1)\n"
    finally:
        live.httpd.shutdown()
        live.httpd.server_close()


@pytest.mark.parametrize("url", [
    "/static/../cli.py",                      # inside the repository, outside `webui/`
    "/static/%2e%2e%2fcli.py",
    "/static/../../etc/passwd",
    "/static/..%2f..%2fetc%2fpasswd",
    "/static//etc/passwd",
    "/media/run/%2e%2e%2f%2e%2e%2fetc%2fpasswd",
    "/media/../../etc/passwd",
    "/media/%2e%2e/x.mp4",
])
def test_traversal_is_refused_with_a_code_not_merely_a_miss(server, url):
    """Accepting any 404 lets a router that simply failed to match the URL pass for a security
    check. The refusal must be explicit.
    """
    status, body = _json(server, url)
    assert status == 400, f"{url} answered {status}, not an explicit refusal"
    assert body["error"]["code"] == "path_outside_root"


def test_the_repository_is_not_the_static_root(server):
    """The specific request the two-root reading of the policy would serve: `/static/../cli.py`
    never leaves the repository, and this file really exists.
    """
    target = PROJECT_ROOT / "h3_48gb" / "cli.py"
    assert target.is_file(), "the URL below is only interesting while this file exists"
    assert web.WEBUI_ROOT.parent == target.parent, "`/static/../cli.py` no longer names it"
    status, body = _json(server, "/static/../cli.py")
    assert status == 400 and body["error"]["code"] == "path_outside_root"


def test_media_serves_a_file_from_a_run_directory(server):
    run = server.outdir / "19-пятнадцать-секунд"
    run.mkdir()
    (run / "h3-centaur-896x576.mp4").write_bytes(b"\x00mp4")
    url = "/media/" + urllib.parse.quote(f"{run.name}/h3-centaur-896x576.mp4")
    status, headers, body = _request(server, url)
    assert status == 200
    assert headers["Content-Type"] == "video/mp4"
    assert body == b"\x00mp4"


def test_media_cannot_step_out_of_one_run_into_another(server):
    """`/media` is bounded by the run's directory, not merely by the outdir: with only the outer
    check this URL stays inside `H3_OUTDIR` and would be served.
    """
    (server.outdir / "run-a").mkdir()
    other = server.outdir / "run-b"
    other.mkdir()
    (other / "secret.mp4").write_bytes(b"x")
    status, body = _json(server, "/media/run-a/../run-b/secret.mp4")
    assert status == 400 and body["error"]["code"] == "path_outside_root"


def test_a_missing_static_file_is_a_json_404_not_an_html_page(server):
    status, body = _json(server, "/static/nope.css")
    assert status == 404
    assert body["ok"] is False and body["error"]["code"] == "not_found"


def test_an_unknown_route_is_a_json_404(server):
    status, body = _json(server, "/api/nothing")
    assert status == 404 and body["error"]["code"] == "not_found"


def test_a_media_url_without_a_file_is_a_json_404(server):
    status, body = _json(server, "/media/run")
    assert status == 404 and body["error"]["code"] == "not_found"


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE"])
def test_an_unsupported_method_answers_json_rather_than_an_html_page(server, method):
    """`BaseHTTPRequestHandler` answers this one itself, in HTML, unless `send_error` is
    overridden -- the one place the "always JSON" contract leaks without a line of our own code
    being involved.
    """
    status, headers, body = _request(server, "/api/state", method=method)
    assert status == 501
    assert headers["Content-Type"] == "application/json"
    assert json.loads(body)["error"]["code"] == "method_not_allowed"
    assert b"<html" not in body.lower() and b"<!DOCTYPE" not in body


def test_responses_carry_a_content_length_and_forbid_caching(server):
    status, headers, body = _request(server, "/api/state")
    assert status == 200
    assert int(headers["Content-Length"]) == len(body)
    assert headers["Cache-Control"] == "no-store"


# -- Step 7: the socket ---------------------------------------------------------------------------


def test_the_server_binds_the_loopback_only(server):
    assert server.httpd.server_address[0] == "127.0.0.1"


def test_the_loopback_constant_is_the_loopback():
    """`make_server` binds `web.LOOPBACK`; the test above therefore only proves the two agree.
    This is the line that says which address that is.
    """
    assert web.LOOPBACK == "127.0.0.1"


def test_make_server_carries_the_three_roots_for_the_submission_routes(tmp_path):
    """`repo`, `models` and `webui` are constructor arguments, so something has to keep them. Task
    6's `check_path_flags` reads them off the server; this pins the wiring now, while the roots are
    still easy to get right.
    """
    outdir = tmp_path / "outdir"
    outdir.mkdir()
    httpd = web.make_server(outdir / "queue", outdir, repo=tmp_path / "repo",
                            models=tmp_path / "models", webui=tmp_path / "webui", port=0)
    try:
        assert httpd.roots == {"repo": tmp_path / "repo", "outdir": outdir,
                               "models": tmp_path / "models"}
        with pytest.raises(CliError) as excinfo:
            web.check_path_flags(["generate", "x", "--outdir", "/etc"], httpd.roots)
        assert excinfo.value.code == "path_outside_root"
    finally:
        httpd.server_close()


def test_make_server_defaults_to_the_real_roots(tmp_path):
    httpd = web.make_server(tmp_path / "queue", tmp_path, port=0)
    try:
        assert httpd.webui == web.WEBUI_ROOT
        assert httpd.roots["repo"] == web.REPO_ROOT
        assert httpd.roots["models"] == web.models_root()
    finally:
        httpd.server_close()


def test_web_module_does_not_import_mlx():
    """The server sits resident all day beside a 36 GB generation. Checked in a subprocess so this
    session's own earlier imports cannot hide a leak.
    """
    code = "import sys; import h3_48gb.web; print('mlx' in sys.modules)"
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                            cwd=str(PROJECT_ROOT))
    assert result.returncode == 0, f"subprocess failed: {result.stderr}"
    assert result.stdout.strip() == "False", "importing h3_48gb.web must not import mlx"


def test_serving_requests_never_imports_mlx(tmp_path):
    """Import-time purity is the easy half. A route that reached `spec_from_args` (which pulls in
    `minimax_h3_mlx.packing` for `--image`) would leave the module import clean and still put the
    whole stack in the server process on the first request.
    """
    script = f"""
import json, sys, threading, urllib.request
from pathlib import Path
sys.path.insert(0, {str(PROJECT_ROOT)!r})
from h3_48gb import queue as q, web

outdir = Path({str(tmp_path)!r}) / "outdir"
(outdir / "run").mkdir(parents=True, exist_ok=True)
(outdir / "run" / "a.jpg").write_bytes(b"jpg")
root = q.layout(outdir / "queue")["root"]
httpd = web.make_server(root, outdir, port=0)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{{httpd.server_address[1]}}"
for url in ("/", "/api/state", "/media/run/a.jpg", "/static/nope.css", "/media/run/../../x"):
    try:
        urllib.request.urlopen(base + url).read()
    except Exception:
        pass
httpd.shutdown()
print("mlx" in sys.modules)
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                            cwd=str(PROJECT_ROOT), timeout=60)
    assert result.returncode == 0, f"subprocess failed: {result.stderr}"
    assert result.stdout.strip().splitlines()[-1] == "False", (
        "serving a request pulled mlx into the server process")


# -- Step 8: the subcommand ------------------------------------------------------------------------


def test_h3_web_takes_an_outdir_and_a_port():
    args = build_parser().parse_args(["web", "--outdir", "/tmp/x", "--port", "9100"])
    assert args.command == "web" and args.port == 9100 and args.outdir == Path("/tmp/x")


def test_the_default_port_is_the_modules_default():
    """Two spellings of 8765 -- the parser's default and `web.DEFAULT_PORT` -- and no import
    linking them (`web` imports `cli`, so `cli` cannot import `web` at module scope).
    """
    assert build_parser().parse_args(["web"]).port == web.DEFAULT_PORT


def test_h3_web_has_no_host_flag():
    """A bind-address flag is the one way this server could stop being loopback-only."""
    with pytest.raises(SystemExit):
        build_parser().parse_args(["web", "--host", "0.0.0.0"])


def test_h3_web_refuses_an_outdir_that_does_not_exist(tmp_path, monkeypatch):
    """`queue.scan` deliberately does not create the directory it reads, so a typo'd outdir would
    otherwise serve an empty queue for ever while the real one fills up somewhere else.

    `serve_forever` is stubbed out even though this test expects never to reach it. Without the
    stub, deleting the check under test does not turn this red -- it makes it **hang**, blocking on
    a real server until the runner is killed, and a test that hangs is a test that reports nothing.
    Asserting the stub was never called is what keeps the refusal itself under test.
    """
    from h3_48gb import cli

    served = []
    monkeypatch.setattr(web._Server, "serve_forever", lambda self: served.append(self))

    with pytest.raises(CliError) as excinfo:
        cli.run_web(tmp_path / "typo", port=0)
    assert excinfo.value.code == "outdir_not_found"
    assert served == [], "the refusal happened after the socket was already serving"


def test_h3_web_binds_the_loopback_and_prints_the_address(tmp_path, capsys, monkeypatch):
    from h3_48gb import cli

    seen = {}

    def stop_immediately(self):
        seen["address"] = self.server_address
        seen["queue_root"] = self.queue_root

    monkeypatch.setattr(web._Server, "serve_forever", stop_immediately)
    report = cli.run_web(tmp_path, port=0)
    assert seen["address"][0] == "127.0.0.1"
    assert seen["queue_root"] == tmp_path / "queue"
    assert report["ok"] is True
    assert f"127.0.0.1:{seen['address'][1]}" in capsys.readouterr().out


def test_h3_web_does_not_create_the_queue_directory(tmp_path, monkeypatch):
    """`h3 web` reads; the worker owns the layout. A server that created `queue/` would make a
    typo'd outdir look like a healthy, permanently empty queue.
    """
    from h3_48gb import cli

    monkeypatch.setattr(web._Server, "serve_forever", lambda self: None)
    cli.run_web(tmp_path, port=0)
    assert not (tmp_path / "queue").exists()


def test_the_three_new_codes_are_in_the_shared_contract():
    from h3_48gb.cli import ERROR_CODES

    assert {"path_outside_root", "prompt_name_invalid", "queue_unwritable"} <= set(ERROR_CODES)


def test_every_status_this_module_maps_belongs_to_a_real_code():
    """`ERROR_STATUS` is keyed by `CliError` codes; a typo there is a silent fallback to 400."""
    from h3_48gb.cli import ERROR_CODES

    unknown = set(web.ERROR_STATUS) - set(ERROR_CODES)
    assert not unknown, f"ERROR_STATUS names codes that do not exist: {unknown}"
