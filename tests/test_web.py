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
import argparse
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

import pytest

from h3_48gb import queue as q
from h3_48gb import web
from h3_48gb.cli import DEFAULT_CHECKPOINT, ERROR_CODES, CliError, build_parser
# `flock` is only honest when the holder is a separate process -- see the module docstring.
from test_cli import bake_adaln_table
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


#: Sentinel for "leave the Host header alone" -- `None` has to mean "send no Host at all".
_KEEP = object()


@dataclass
class _Live:
    """A running server plus the directories it was pointed at."""

    httpd: object
    port: int
    queue_root: Path
    outdir: Path
    webui: Path
    repo: Path
    models: Path


def _serve(queue_root, outdir, **kwargs) -> _Live:
    httpd = web.make_server(queue_root, outdir, port=0, **kwargs)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return _Live(httpd=httpd, port=httpd.server_address[1], queue_root=Path(queue_root),
                 outdir=Path(outdir), webui=Path(httpd.webui),
                 repo=Path(httpd.roots["repo"]), models=Path(httpd.roots["models"]))


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


def _request(live: _Live, url: str, method: str = "GET", host=_KEEP):
    """`(status, headers, body_bytes)` for a raw URL, sent verbatim.

    `http.client` does not normalise the request target, which is what makes `/static/../cli.py`
    reach the server as written instead of being collapsed by the client.

    `host` replaces the `Host` header (`None` removes it), for the rebinding tests; left alone it
    is whatever `http.client` builds, i.e. this server's real address.
    """
    connection = http.client.HTTPConnection(web.LOOPBACK, live.port, timeout=10)
    try:
        if host is _KEEP:
            connection.request(method, url)
        elif host is None:
            connection.putrequest(method, url, skip_host=True, skip_accept_encoding=True)
            connection.endheaders()
        else:
            connection.putrequest(method, url, skip_host=True, skip_accept_encoding=True)
            connection.putheader("Host", host)
            connection.endheaders()
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def _json(live: _Live, url: str, method: str = "GET", host=_KEEP):
    status, headers, body = _request(live, url, method, host=host)
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


@pytest.mark.parametrize("url", [
    "/media/run-a/../run-b/secret.mp4",   # the spelling the first version already refused
    "/media//run-b/secret.mp4",           # empty first segment -> `<outdir>/""` is the outdir
    "/media/./run-b/secret.mp4",          # `.` -> the outdir again
    "/media/%2e/run-b/secret.mp4",        # and the encoded `.`
])
def test_media_cannot_step_out_of_one_run_into_another(server, url):
    """`/media` is bounded by **one run's** directory, not merely by the outdir.

    The last three spellings are review circle 1's finding, and they were all served with a 200
    before the fix: each collapses `<outdir>/<run>` back onto the outdir itself, after which the
    inner "is the file inside the run directory" check compares the outdir with the outdir and
    lets the entire output tree through. The original test only covered the `..` spelling, so it
    stayed green over the hole.
    """
    (server.outdir / "run-a").mkdir()
    other = server.outdir / "run-b"
    other.mkdir()
    (other / "secret.mp4").write_bytes(b"x")
    status, body = _json(server, url)
    assert status == 400, f"{url} answered {status}"
    assert body["error"]["code"] == "path_outside_root"


@pytest.mark.parametrize("spelling", ["queue", "QUEUE", "Queue", "QuEuE"])
def test_media_does_not_serve_the_queue(server, spelling):
    """`queue/` **is** a direct child of the outdir, so the direct-child rule alone lets this
    through. It is not a run: the page reads the queue over `/api/state`, which returns jobs, not
    whatever bytes happen to be sitting in `queue/logs/`.

    The three capitalised spellings are circle 2's finding. `Path.resolve()` does not canonicalise
    case, this machine's APFS volume is case-insensitive, so a comparison of resolved *paths* was
    decided by how the request spelled the directory: `queue` was refused and `QUEUE` returned the
    job file. Comparison is now by inode.
    """
    (server.queue_root / "pending" / "20260811-000000-x-0000.json").write_text('{"a": 1}')
    status, body = _json(server, f"/media/{spelling}/pending/20260811-000000-x-0000.json")
    assert status == 400, f"/media/{spelling}/… answered {status}"
    # `path_outside_root` exactly, not "one of two". Accepting `media_type_not_allowed` as well
    # made this test survive the removal of the queue check -- the request would simply travel one
    # step further and be refused by the allowlist for being `.json`. The docstring above claims
    # to cover the case-spelling regression; only the exact code makes that true here rather than
    # in one other test id.
    assert body["error"]["code"] == "path_outside_root"


@pytest.mark.parametrize("spelling", ["queue", "QUEUE", "QuEuE"])
def test_the_queue_is_refused_even_for_a_file_type_media_serves(server, spelling):
    """The allowlist would refuse `.json` and `.log` whatever directory they sat in, so it alone
    does not prove the queue is excluded. A `.jpg` inside the queue is the case that separates the
    two rules -- and it must still be refused, by identity.
    """
    (server.queue_root / "logs").mkdir(exist_ok=True)
    (server.queue_root / "logs" / "sneak.jpg").write_bytes(b"\xff\xd8x")
    status, body = _json(server, f"/media/{spelling}/logs/sneak.jpg")
    assert status == 400, f"/media/{spelling}/logs/sneak.jpg answered {status}"
    assert body["error"]["code"] == "path_outside_root"


def test_samefile_sees_through_case_where_path_equality_does_not(server):
    """Documents the *primitive*, not the route: `_is_same_file` in isolation, and the fact that
    the obvious alternative disagrees with it on this volume.

    Named for what it checks. The route-level guarantee is
    `test_the_queue_is_refused_even_for_a_file_type_media_serves`, which is what actually fails if
    the call site stops using this helper -- a reader should not have to work out that this test
    is not that one.
    """
    upper = server.outdir / "QUEUE"
    assert upper.resolve() != server.queue_root.resolve(), "not a case-insensitive volume"
    assert web._is_same_file(upper, server.queue_root), "samefile did not see through the case"


# -- circle 3: what the five checks rest on ------------------------------------------------------


def test_the_suffix_and_the_bytes_come_from_the_same_path(server):
    """The property underneath the whole `/media` policy: `_serve_file` takes the suffix from the
    **resolved** path, which is the same path it then reads.

    A symbolic link is the case that separates the two readings. `frame.mp4 -> notes.txt` inside a
    run directory has an allowed name and a forbidden target; checking `relative` would allow it
    and then hand back `notes.txt`'s bytes -- name from one file, contents from another. This is
    what defeated all forty of circle 3's spellings, and it is not any of the five numbered checks
    in `_media`, so nothing else pins it.
    """
    run = server.outdir / "19-real-run"
    run.mkdir(exist_ok=True)
    (run / "notes.txt").write_text("not a clip")
    (run / "frame.mp4").symlink_to(run / "notes.txt")
    status, body = _json(server, "/media/19-real-run/frame.mp4")
    assert status == 400, f"answered {status} -- the suffix was taken from the link, not the target"
    assert body["error"]["code"] == "media_type_not_allowed"
    assert body["error"]["detail"]["suffix"] == ".txt"


def test_a_symlink_with_an_allowed_suffix_still_cannot_leave_the_run(server):
    """The same resolution, the other half: an allowed target type does not buy an escape."""
    run = server.outdir / "19-real-run"
    run.mkdir(exist_ok=True)
    other = server.outdir / "other-run"
    other.mkdir(exist_ok=True)
    (other / "secret.mp4").write_bytes(b"secret")
    (run / "innocent.mp4").symlink_to(other / "secret.mp4")
    status, body = _json(server, "/media/19-real-run/innocent.mp4")
    assert status == 400 and body["error"]["code"] == "path_outside_root"


def test_a_symlink_inside_the_run_is_still_served(server):
    """Paired with both: refusing every link would satisfy them and break an ordinary one."""
    run = server.outdir / "19-real-run"
    run.mkdir(exist_ok=True)
    (run / "real.mp4").write_bytes(b"clip")
    (run / "latest.mp4").symlink_to(run / "real.mp4")
    status, _, body = _request(server, "/media/19-real-run/latest.mp4")
    assert status == 200 and body == b"clip"


# -- circle 3: a name the filesystem itself refuses ----------------------------------------------


@pytest.mark.parametrize("url", [
    "/media/19-real-run/{long}.mp4",     # allowed suffix: used to reach is_file() and raise
    "/media/19-real-run/{long}.json",    # forbidden suffix: refused earlier, so it already worked
    "/media/{long}/clip.mp4",            # the run segment
    "/static/{long}.css",                # and the same defect on the other route
])
def test_an_over_long_name_is_a_404_not_a_crash(server, url):
    """`ENAMETOOLONG` is not one of the errnos `pathlib` swallows, so `is_file()` raised it
    straight into the handler's `internal_error` net: a 300-character `.json` answered 400 and a
    300-character `.mp4` answered 500, for the same input class. Reporting caller-controlled input
    as a bug in this server is exactly what `resolve_within`'s docstring forbids.
    """
    (server.outdir / "19-real-run").mkdir(exist_ok=True)
    status, body = _json(server, url.format(long="a" * 300))
    assert status in (400, 404), f"answered {status}: {body}"
    assert body["error"]["code"] in {"not_found", "media_type_not_allowed"}
    assert body["error"]["code"] != "internal_error"


def test_an_unreadable_file_is_a_404_rather_than_an_oracle(server):
    """`read_bytes` failing must answer the same as "not there": telling the two apart tells a
    caller which files exist but are locked down.
    """
    if os.geteuid() == 0:
        pytest.skip("root reads a 000 file regardless of its mode")
    run = server.outdir / "19-real-run"
    run.mkdir(exist_ok=True)
    clip = run / "locked.mp4"
    clip.write_bytes(b"clip")
    clip.chmod(0o000)
    try:
        status, body = _json(server, "/media/19-real-run/locked.mp4")
        assert status == 404 and body["error"]["code"] == "not_found"
    finally:
        clip.chmod(0o644)


# -- circle 3: the allowlist cannot be opened by omission ----------------------------------------


def test_serving_a_file_requires_saying_which_types(tmp_path):
    """`suffixes` is required and keyword-only. It used to default to "anything", so a route added
    later that forgot the argument would serve its whole directory silently -- an open default is
    a class of bug, and the class is what had to go.
    """
    with pytest.raises(TypeError):
        web._serve_file(tmp_path, "x.mp4")


def test_any_suffix_is_a_decision_a_route_writes_down(tmp_path):
    (tmp_path / "a.weird").write_bytes(b"x")
    assert ".weird" in web.ANY_SUFFIX and ".anything-at-all" in web.ANY_SUFFIX
    status, _, body = web._serve_file(tmp_path, "a.weird", suffixes=web.ANY_SUFFIX)
    assert status == 200 and body == b"x"


@pytest.mark.parametrize("name,suffix", [("job.json", ".json"), ("run.log", ".log"),
                                         ("prompt.txt", ".txt"), ("h3-x.safetensors",
                                                                  ".safetensors"),
                                         ("notes.md", ".md"), ("script.sh", ".sh")])
def test_media_serves_only_clips_and_frames(server, name, suffix):
    """The allowlist, inside a perfectly legitimate run directory -- otherwise it would be covered
    only through the queue tests and could be deleted without any of them noticing.

    `.safetensors` is not hypothetical: `<outdir>/checkpoints` is the default `--checkpoint-dir`,
    so before this rule `/media/checkpoints/h3-*.safetensors` handed out multi-gigabyte resume
    weights, read whole into this process's memory.
    """
    run = server.outdir / "19-real-run"
    run.mkdir(exist_ok=True)
    (run / name).write_bytes(b"x")
    # `_request`, not `_json`: a `.json` file served by mistake comes back *as* JSON, so decoding
    # first turns "the file leaked" into an unreadable decode error instead of a status mismatch.
    status, _, raw = _request(server, f"/media/19-real-run/{name}")
    assert status == 400, f"{name} answered {status} with {raw[:40]!r}"
    body = json.loads(raw)
    assert body["error"]["code"] == "media_type_not_allowed"
    assert body["error"]["detail"]["suffix"] == suffix


@pytest.mark.parametrize("name", ["absent.json", "absent.log", "absent.safetensors"])
def test_media_refuses_a_forbidden_type_before_anyone_looks_for_it(server, name):
    """The refusal must not double as an existence oracle: the extension is checked *before*
    `is_file`, so a caller cannot tell `queue/logs/real.log` from `queue/logs/guess.log` by whether
    the answer is `media_type_not_allowed` or `not_found`. Ordering is invisible in the code and
    only this test pins it.
    """
    run = server.outdir / "19-real-run"
    run.mkdir(exist_ok=True)
    assert not (run / name).exists()
    status, body = _json(server, f"/media/19-real-run/{name}")
    assert status == 400 and body["error"]["code"] == "media_type_not_allowed", (
        "a missing file answered differently from a present one, which is an oracle")


@pytest.mark.parametrize("name", ["clip.mp4", "frame.jpg", "frame.jpeg", "frame.png", "sound.wav",
                                  "CLIP.MP4", "Frame.JPG"])
def test_media_still_serves_every_type_the_page_needs(server, name):
    """The other half: an allowlist that refused everything would pass the test above. The upper
    case spellings are here because the suffix check lowercases, and a run really can contain
    `FRAME.JPG` -- refusing it would be the allowlist breaking a legitimate file.
    """
    run = server.outdir / "19-real-run"
    run.mkdir(exist_ok=True)
    (run / name).write_bytes(b"bytes")
    status, _, body = _request(server, f"/media/19-real-run/{name}")
    assert status == 200 and body == b"bytes"


def test_media_serves_a_preview_frame_nested_under_a_run(server):
    """`<run>/checkpoints/step05.jpg` is a legitimate preview frame. The rules above must not have
    made the ordinary nested case collateral damage -- forbidding nesting would break real access,
    and forbidding `checkpoints/` by name would be the denylist mistake again.
    """
    nested = server.outdir / "19-real-run" / "checkpoints"
    nested.mkdir(parents=True, exist_ok=True)
    (nested / "step05.jpg").write_bytes(b"\xff\xd8frame")
    status, _, body = _request(server, "/media/19-real-run/checkpoints/step05.jpg")
    assert status == 200 and body == b"\xff\xd8frame"


def test_media_serves_a_file_nested_inside_a_run(server):
    """The direct-child rule constrains the **run**, not the file under it. A run directory has
    subdirectories of its own (`checkpoints/`), and a rule that flattened the tail as well would
    refuse ordinary contents for no gain: everything below the run directory is still inside it.
    """
    nested = server.outdir / "a" / "b"
    nested.mkdir(parents=True)
    (nested / "x.mp4").write_bytes(b"x")
    status, _, body = _request(server, "/media/a/b/x.mp4")
    assert status == 200 and body == b"x"


def test_media_still_serves_a_real_run(server):
    """Paired with all of the above: a rule that refused every `/media` URL would satisfy them."""
    run = server.outdir / "19-run"
    run.mkdir()
    (run / "h3-x-preview-step05.jpg").write_bytes(b"\xff\xd8jpg")
    status, headers, body = _request(server, "/media/19-run/h3-x-preview-step05.jpg")
    assert status == 200 and headers["Content-Type"] == "image/jpeg" and body == b"\xff\xd8jpg"


# -- circle 1: the Host header (DNS rebinding) ---------------------------------------------------


@pytest.mark.parametrize("host", ["evil.example", "evil.example:80", "attacker.test:1",
                                  "127.0.0.1:1", "localhost", "127.0.0.1", None])
def test_a_request_for_someone_elses_host_is_refused(server, host):
    """DNS rebinding: a page on `evil.example` whose name resolves to `127.0.0.1` on its second
    lookup is same-origin with itself, so the browser sends the request and hands the body back to
    the attacker's script. Binding the loopback does not help -- the browser is already on it.
    `Host` is the one header that script cannot forge, so it is the whole defence.

    The port-less spellings are in the list because they are what an attacker gets for free on
    port 80, and a check that compared only the hostname would accept them.
    """
    status, body = _json(server, "/api/state", host=host)
    assert status == 403, f"Host {host!r} answered {status}"
    assert body["error"]["code"] == "host_not_allowed"


@pytest.mark.parametrize("template", ["127.0.0.1:{port}", "localhost:{port}"])
def test_the_two_spellings_of_this_machine_are_accepted(server, template):
    """The other half: a check that refused everything would pass the test above, and the page
    itself is opened as one of these two.
    """
    status, body = _json(server, "/api/state", host=template.format(port=server.port))
    assert status == 200 and body["ok"] is True


def test_the_host_check_runs_before_the_route(server):
    """A rebinding request must not be able to reach a route at all -- not the queue, not a file,
    not even a 404 that confirms what exists. Checking inside each route is a check the next route
    forgets.
    """
    for url in ("/", "/static/app.js", "/media/run/x.mp4", "/api/nothing", "/static/../cli.py"):
        status, body = _json(server, url, host="evil.example")
        assert status == 403 and body["error"]["code"] == "host_not_allowed", url


# -- circle 1: a byte that is not a path ---------------------------------------------------------


@pytest.mark.parametrize("url", ["/static/%00../cli.py", "/static/a%00b.css",
                                 "/media/run%00/x.mp4", "/media/run/x%00.mp4"])
def test_a_nul_byte_is_a_refusal_not_a_crash(server, url):
    """`Path.resolve()` raises `ValueError` on an embedded NUL. Reaching the `internal_error` net
    keeps the JSON contract but reports an attacker-controlled input as a bug in this server; a
    500 is a thing someone investigates, and this is a thing someone refuses.
    """
    status, body = _json(server, url)
    assert status == 400, f"{url} answered {status}"
    assert body["error"]["code"] == "path_outside_root"


def test_a_tilde_naming_no_such_user_is_a_refusal_too(tmp_path):
    """`expanduser()` raises `RuntimeError` for an unknown user, from the same line."""
    with pytest.raises(CliError) as excinfo:
        web.resolve_within("~nosuchuser1234/x", _roots(tmp_path), write=False)
    assert excinfo.value.code == "path_outside_root"


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


@pytest.mark.parametrize("method", ["PATCH", "OPTIONS", "TRACE"])
def test_an_unsupported_method_answers_json_rather_than_an_html_page(server, method):
    """`BaseHTTPRequestHandler` answers this one itself, in HTML, unless `send_error` is
    overridden -- the one place the "always JSON" contract leaks without a line of our own code
    being involved.

    The three methods here used to be `POST`, `PUT` and `DELETE`; task 6 gave those handlers, so
    they answer 404 through `_respond` now (see
    `test_a_write_method_on_an_unknown_route_is_a_json_404`) and these three took their place --
    still unimplemented, still reaching the base class's HTML page without the override.
    """
    status, headers, body = _request(server, "/api/state", method=method)
    assert status == 501
    assert headers["Content-Type"] == "application/json"
    # `method_not_implemented`, not `method_not_allowed`: the status the standard library raises
    # here is 501, and the two used to share a name that said 405 while the status said 501.
    assert json.loads(body)["error"]["code"] == "method_not_implemented"
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


def test_the_new_codes_are_in_the_shared_contract():
    from h3_48gb.cli import ERROR_CODES

    assert {"path_outside_root", "prompt_name_invalid", "queue_unwritable"} <= set(ERROR_CODES)


def test_every_router_code_is_in_the_shared_contract():
    """One dictionary on the wire, not two. `ROUTER_CODES` carries the two most ordinary failures
    there are -- a typo in the address and the wrong method -- and task 7's page turns a `code`
    into a Russian sentence.
    """
    from h3_48gb.cli import ERROR_CODES

    missing = set(web.ROUTER_CODES.values()) - set(ERROR_CODES)
    assert not missing, f"the router answers with codes ERROR_CODES does not document: {missing}"


def test_the_router_only_ever_answers_with_a_documented_code():
    """The catch-all branches too: `414`, `431` and `505` are raised by the standard library on
    this server's behalf and no line here mentions them by number.
    """
    answered = {web._router_code(status) for status in range(400, 600)}
    assert answered <= set(web.ROUTER_CODES.values()), (
        f"undocumented codes reachable: {answered - set(web.ROUTER_CODES.values())}")


@pytest.mark.parametrize("status,code", [(403, "host_not_allowed"), (404, "not_found"),
                                         (501, "method_not_implemented"), (414, "bad_request"),
                                         (505, "bad_request"), (502, "internal_error")])
def test_the_router_code_matches_its_status(status, code):
    """A name that disagrees with its status is worse than no name: `405` and `501` used to share
    `method_not_allowed`, so a caller reading the code was told the wrong thing about the response
    it was holding.
    """
    assert web._router_code(status) == code


def test_every_status_this_module_maps_belongs_to_a_real_code():
    """`ERROR_STATUS` is keyed by `CliError` codes; a typo there is a silent fallback to 400.

    `PLANNED_CODES` is the one exemption, and it is a short, named list of data rather than a
    widened rule.
    """
    from h3_48gb.cli import ERROR_CODES

    unknown = set(web.ERROR_STATUS) - set(ERROR_CODES) - web.PLANNED_CODES
    assert not unknown, f"ERROR_STATUS names codes that do not exist: {unknown}"


def test_job_not_pending_answers_409():
    """Unconditional, so the failure mode "task 6 adds the code and forgets the status" is not
    tested for -- it is made impossible. The status is already here; the code arrives later.

    This replaces a conditional test that asserted nothing until the day it mattered.
    """
    assert web.ERROR_STATUS["job_not_pending"] == 409


def test_no_planned_code_has_already_arrived():
    """Keeps `PLANNED_CODES` from turning into a permanent hole in the check above: once task 6
    adds `job_not_pending` to `ERROR_CODES`, this fails until the entry is dropped from the
    exemption, at which point the ordinary check covers it again.
    """
    from h3_48gb.cli import ERROR_CODES

    landed = web.PLANNED_CODES & set(ERROR_CODES)
    assert not landed, (
        f"these codes now exist and no longer need an exemption -- drop them from "
        f"PLANNED_CODES: {sorted(landed)}")


# == Task 6 ======================================================================================
#
# Submission, editing, prompts and the cost estimate. Three rules run through every test below:
#
# * **Nothing here runs a real generation.** Validation is a `generate --dry-run --json`
#   subprocess, which builds a `RunSpec` and returns without loading a weight; where a test needs
#   to see the command line rather than its answer, it hands `validate_args` a fake interpreter
#   that only echoes its argv.
# * **The estimate is checked on four measured points and both bit widths**, not on one. A single
#   point is satisfied by a function that returns a constant.
# * **A refusal is checked by its code and its status**, never by "not 200" -- the same rule the
#   read routes already follow.


# -- Step 1: the cost estimate --------------------------------------------------------------------


@pytest.mark.parametrize("w,h,sec,steps,bits,total_min,peak", [
    (448, 288, 10, 8, 8, 13.3, 24.8),
    (896, 576, 10, 8, 8, 72.3, 32.9),
    (896, 576, 15, 8, 8, 148.8, 35.7),
    (896, 576, 15, 8, 4, 148.8, 25.7),
])
def test_estimate_reproduces_the_measured_runs(tmp_path, w, h, sec, steps, bits, total_min, peak):
    """The four runs of 2026-08-11 from the design spec, plus the 4-bit reading of the last one.

    Four points and two bit widths on purpose: one point is reproduced exactly as well by
    `return 13.3`, and one bit width by ignoring `quant_config.json` altogether. The two canvases
    pin the quadratic in `rows`, the two durations pin the `81*sec` term, and the pair that differs
    only in `bits` pins the weights constant while everything else is held still.
    """
    ckpt = tmp_path / "ckpt"
    (ckpt / "transformer").mkdir(parents=True)
    (ckpt / "transformer" / "quant_config.json").write_text(json.dumps({"bits": bits}))
    got = web.estimate(["generate", "x", "--width", str(w), "--height", str(h),
                        "--duration", str(sec), "--steps", str(steps)], checkpoint=ckpt)
    assert got["forwards"] == steps - 1
    assert abs(got["seconds"] / 60 - total_min) / total_min < 0.12, got
    assert abs(got["peak_gb"] - peak) < 1.5, got


def test_the_overhead_term_is_present_and_grows_with_the_canvas():
    """Without it the form promises diffusion time and the human waits ten minutes longer.

    Both halves matter. A fixed constant satisfies "the overhead is there"; only the comparison
    between two canvases says it tracks `W*H*sec`, which is what the four measured runs show and
    what the rejected constant-600 model got wrong.
    """
    small = web.estimate(["generate", "x", "--width", "448", "--height", "288",
                          "--duration", "10", "--steps", "8"], checkpoint=None)
    large = web.estimate(["generate", "x", "--width", "896", "--height", "576",
                          "--duration", "10", "--steps", "8"], checkpoint=None)
    assert small["overhead_seconds"] < large["overhead_seconds"]
    assert 100 < small["overhead_seconds"] < 200
    assert small["seconds"] > small["diffusion_seconds"], (
        "`seconds` must be the whole run, not the diffusion the forward model covers")


def test_a_checkpoint_without_quant_config_is_treated_as_four_bit(tmp_path):
    """The smaller guess is the safe one: assuming 8 bits would add ten gigabytes to every
    estimate made against a checkpoint whose record is simply missing.
    """
    ckpt = tmp_path / "ckpt"
    ckpt.mkdir()
    assert web.quant_bits(ckpt) == 4
    assert web.quant_bits(None) == 4
    baseline = web.estimate(["generate", "x", "--width", "896", "--height", "576",
                             "--duration", "15", "--steps", "8"], checkpoint=ckpt)
    assert baseline["bits"] == 4
    assert abs(baseline["peak_gb"] - 25.7) < 1.5, baseline


@pytest.mark.parametrize("relative", ["transformer/quant_config.json", "quant_config.json"])
def test_both_real_checkpoint_layouts_are_read(tmp_path, relative):
    """`~/models/h3-converted` is a pipeline directory and keeps the record under `transformer/`;
    `~/models/h3-8bit` *is* a transformer directory and keeps it at its own root. Both exist on
    this machine, so `--checkpoint` may legitimately name either.
    """
    ckpt = tmp_path / "ckpt"
    (ckpt / relative).parent.mkdir(parents=True, exist_ok=True)
    (ckpt / relative).write_text(json.dumps({"bits": 8, "group_size": 64}))
    assert web.quant_bits(ckpt) == 8


def test_a_malformed_quant_config_does_not_break_the_estimate(tmp_path):
    """An estimate is drawn beside a form. Refusing to draw it because a JSON file is truncated
    would be a worse outcome than drawing the 4-bit number and saying so in `bits`.
    """
    ckpt = tmp_path / "ckpt"
    (ckpt / "transformer").mkdir(parents=True)
    (ckpt / "transformer" / "quant_config.json").write_text("{not json")
    assert web.quant_bits(ckpt) == 4


def test_the_estimate_prefers_the_dry_run_reports_canvas(tmp_path):
    """With `--image` and no explicit canvas, only `resolve_canvas` knows the size -- and it
    imports mlx. The report from the subprocess is where the real numbers come from, so a job's
    stored estimate describes the canvas that will actually run rather than the default.
    """
    args = ["generate", "x", "--image", str(tmp_path / "frame.png")]
    default = web.estimate(args, checkpoint=None)
    from_report = web.estimate(args, checkpoint=None,
                               report={"canvas": "448x288", "duration_seconds": 10.0,
                                       "grid_points": 8})
    assert (default["width"], default["height"]) == (896, 512)
    assert (from_report["width"], from_report["height"]) == (448, 288)
    assert from_report["forwards"] == 7
    assert from_report["seconds"] < default["seconds"]


def test_estimating_an_unparseable_argument_list_is_args_invalid():
    with pytest.raises(CliError) as excinfo:
        web.estimate(["generate", "x", "--widht", "896"], checkpoint=None)
    assert excinfo.value.code == "args_invalid"
    assert "--widht" in excinfo.value.detail["stderr"]


def test_the_parser_helper_does_not_swallow_a_real_refusal(monkeypatch):
    """`CliError` subclasses `SystemExit`, so a blanket `except SystemExit` around `parse_args`
    would relabel every refusal raised underneath it as `args_invalid` and lose its code.
    """
    def explode(self, argv):
        raise CliError("path_outside_root", "boom", {})

    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", explode)
    with pytest.raises(CliError) as excinfo:
        web._parse_args(["generate", "x"])
    assert excinfo.value.code == "path_outside_root"


# -- Step 3: helpers for the write routes ---------------------------------------------------------


@pytest.fixture
def queue_server(tmp_path):
    """A server whose **three** roots are all temporary.

    Unlike the `server` fixture, which points at the real repository so that `/static/../cli.py`
    has something real to escape into, submission tests need to name a checkpoint, an outdir and a
    prompts directory that they control -- and a job naming `~/models` would be a test that reads
    this machine's weights.
    """
    outdir = tmp_path / "outdir"
    outdir.mkdir()
    repo = tmp_path / "repo"
    (repo / "prompts").mkdir(parents=True)
    models = tmp_path / "models"
    # A checkpoint, not just a directory named like one: a fake checkpoint with no readable
    # `transformer/adaln_cache.safetensors` is now refused (`checkpoint_without_adaln`) before
    # anything else about the job is judged, and these tests are about the other refusals.
    bake_adaln_table(models / "ckpt")
    webui = tmp_path / "webui"
    webui.mkdir()
    root = q.layout(outdir / "queue")["root"]
    live = _serve(root, outdir, repo=repo, models=models, webui=webui)
    yield live
    live.httpd.shutdown()
    live.httpd.server_close()


def _call(live: _Live, method: str, url: str, payload=None, *, headers=None):
    """`(status, parsed_json)` for a request that may carry a JSON body.

    Every answer is required to be JSON, refusals included -- the same contract `_json` enforces
    for the read routes, restated here because a write route is exactly where a stray HTML error
    page would first appear.
    """
    connection = http.client.HTTPConnection(web.LOOPBACK, live.port, timeout=180)
    try:
        sent = dict(headers or {})
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            sent.setdefault("Content-Type", "application/json")
        connection.request(method, url, body=body, headers=sent)
        response = connection.getresponse()
        raw = response.read()
        assert response.getheader("Content-Type") == "application/json", (
            f"{method} {url} answered {response.getheader('Content-Type')!r}; "
            f"the contract is JSON everywhere")
        return response.status, json.loads(raw)
    finally:
        connection.close()


def _job_args(live: _Live, *extra, tag="ночь", prompt="котик на подоконнике"):
    """A minimal argument list `h3 generate --dry-run` accepts, with every path inside a root.

    `--steps` is left at its default: with no baked AdaLN table in the fake checkpoint,
    `RunSpec.__post_init__` requires exactly `BAKED_GRID_POINTS`, and spelling that out here would
    couple every submission test to a constant none of them are about.
    """
    args = ["generate", "--outdir", str(live.outdir), "--checkpoint", str(live.models / "ckpt"),
            "--tag", tag, *extra]
    # `--prompt-file` and a positional prompt together are `prompt_both_given`, so naming a file
    # drops the positional one rather than making every caller remember to.
    if prompt is not None and "--prompt-file" not in extra:
        args.insert(1, prompt)
    return args


def _pending(live: _Live) -> list:
    jobs, _ = q.scan(live.queue_root)
    return [job for job in jobs if job.state == "pending"]


#: One estimate request body, reused where a test has to spell out `Content-Length` by hand.
_ESTIMATE_BODY = json.dumps({"args": ["generate", "кот"]}).encode("utf-8")


def _raw_exchange(live: _Live, raw: bytes):
    """`(status, parsed_json)` for bytes written straight onto the socket.

    `http.client` deduplicates and reorders headers, so a request with two `Origin` lines cannot be
    built through it -- and two `Origin` lines are exactly what has to reach the server here.
    """
    import socket

    with socket.create_connection((web.LOOPBACK, live.port), timeout=30) as sock:
        sock.sendall(raw)
        chunks = []
        while True:
            piece = sock.recv(65536)
            if not piece:
                break
            chunks.append(piece)
    head, _, body = b"".join(chunks).partition(b"\r\n\r\n")
    status = int(head.split(b"\r\n", 1)[0].split()[1])
    assert b"application/json" in head, f"the contract is JSON everywhere; got {head!r}"
    return status, json.loads(body)


def _write_png(path, width=64, height=64) -> None:
    """A real PNG, because `--image` is decoded by PIL before the canvas is derived from it."""
    import struct
    import zlib

    def chunk(kind, data):
        payload = kind + data
        return struct.pack(">I", len(data)) + payload + struct.pack(">I", zlib.crc32(payload))

    rows = b"".join(b"\x00" + b"\x80\x40\x20" * width for _ in range(height))
    Path(path).write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b""))


def _fake_python(tmp_path, body: str, name="fake-python") -> Path:
    """An executable that stands in for the interpreter `validate_args` spawns.

    This is how the *command line* gets tested rather than only its answer: the real subprocess
    can only be asked whether it agreed, while a stand-in can report exactly what it was asked.
    A shebang naming this interpreter keeps it a plain Python script.
    """
    script = tmp_path / name
    script.write_text(f"#!{sys.executable}\nimport json, sys\n{body}\n")
    script.chmod(0o755)
    return script


# -- Step 3: posting a job ------------------------------------------------------------------------


def test_posting_a_job_queues_it_with_a_snapshot_of_the_prompt(queue_server):
    """The snapshot is the whole reason prompts are files: an edit between queueing and running
    must not change what runs. The server passes the *text*; `queue.submit` writes the copy,
    because the copy's path contains an id that does not exist until `submit` claims it.
    """
    source = queue_server.repo / "prompts" / "scene.txt"
    source.write_text("кот сидит на подоконнике", encoding="utf-8")

    status, answer = _call(queue_server, "POST", "/api/jobs",
                           {"args": _job_args(queue_server, "--prompt-file", str(source),
                                              prompt=None),
                            "note": "первая ночная"})
    assert status == 200, answer
    job = answer["job"]

    snapshot = queue_server.queue_root / "prompts" / f"{job['id']}.txt"
    assert snapshot.read_text(encoding="utf-8") == "кот сидит на подоконнике"
    assert job["args"][job["args"].index("--prompt-file") + 1] == str(snapshot), (
        "the queued job must point at the snapshot, not at the shared file")
    assert job["prompt_source"] == "prompts/scene.txt"
    assert job["prompt_sha256"]
    assert job["note"] == "первая ночная"
    assert [j.id for j in _pending(queue_server)] == [job["id"]]

    source.write_text("совсем другой промпт", encoding="utf-8")
    assert snapshot.read_text(encoding="utf-8") == "кот сидит на подоконнике", (
        "editing the shared prompt after queueing changed what the job will run")


@pytest.mark.parametrize("field", ["prompt_text", "prompt_source", "estimate", "priority"])
def test_a_body_field_this_route_does_not_take_is_refused_rather_than_ignored(queue_server, field):
    """The snapshot comes from the file `--prompt-file` names and from nowhere else -- a prompt
    supplied beside a *different* flag would record one text and run another, which is worse than
    having no snapshot. But ignoring the field silently is only half a decision: whoever wrote the
    caller sends it, is answered 200, and goes away sure the server used it. Same defect as
    `suffixes=None` meaning "serve anything".
    """
    source = queue_server.repo / "prompts" / "scene.txt"
    source.write_text("настоящий текст", encoding="utf-8")
    body = {"args": _job_args(queue_server, "--prompt-file", str(source), prompt=None),
            "note": "", field: "подложенный текст"}
    status, answer = _call(queue_server, "POST", "/api/jobs", body)
    assert status == 400, answer
    assert answer["error"]["code"] == "args_invalid", answer
    assert answer["error"]["detail"]["unknown"] == [field]
    assert _pending(queue_server) == []


def test_the_snapshot_text_comes_from_the_file(queue_server):
    """The other half of the test above: with the field refused, the only source left is the file,
    and the snapshot has to be byte-identical to it.
    """
    source = queue_server.repo / "prompts" / "scene.txt"
    source.write_text("настоящий текст", encoding="utf-8")
    status, answer = _call(queue_server, "POST", "/api/jobs",
                           {"args": _job_args(queue_server, "--prompt-file", str(source),
                                              prompt=None),
                            "note": ""})
    assert status == 200, answer
    snapshot = queue_server.queue_root / "prompts" / f"{answer['job']['id']}.txt"
    assert snapshot.read_text(encoding="utf-8") == "настоящий текст"


def test_a_geometry_the_cli_refuses_is_refused_here_with_the_same_code(queue_server):
    """One set of rules, in the CLI, reached through the subprocess -- so the page shows the code
    the command line would have shown, not a server-side paraphrase of it.
    """
    status, answer = _call(queue_server, "POST", "/api/jobs",
                           {"args": _job_args(queue_server, "--width", "100", "--height", "288"),
                            "note": ""})
    assert status == 400, answer
    assert answer["error"]["code"] == "geometry_not_multiple_of_32", answer
    assert _pending(queue_server) == []


def test_an_unknown_flag_becomes_args_invalid_not_a_crash(queue_server):
    """argparse answers a typo with `SystemExit(2)` and usage on stderr. Uncaught that is a 500
    for what is plainly the caller's mistake; argparse's own sentence names the typo.
    """
    status, answer = _call(queue_server, "POST", "/api/jobs",
                           {"args": _job_args(queue_server, "--widht", "896"), "note": ""})
    assert status == 400, answer
    assert answer["error"]["code"] == "args_invalid", answer
    assert "--widht" in answer["error"]["detail"]["stderr"]
    assert _pending(queue_server) == []


@pytest.mark.parametrize("args", [["worker"], ["web"], ["status"], ["resume", "x"],
                                  ["generate", "x", "--no-checkpoint"]])
def test_only_generate_with_a_checkpoint_may_be_queued(queue_server, args):
    """A queued `worker` would put a worker inside the worker; a queued `web`, a server inside the
    server. `--no-checkpoint` is the same rule seen from the other side: an interrupted job that
    cannot continue defeats the queue.
    """
    status, answer = _call(queue_server, "POST", "/api/jobs", {"args": args, "note": ""})
    assert status == 400, answer
    assert answer["error"]["code"] == "command_not_allowed", answer
    assert _pending(queue_server) == []


def test_a_tag_that_builds_a_path_out_of_the_outdir_is_refused(queue_server, tmp_path):
    """`--tag` takes no path and yet composes one: the output name is
    `outdir / f"h3-{tag}-{W}x{H}"`. It has no `type=Path`, so neither `PATH_FLAGS` nor the test
    that reads the flag list back out of the parser can see it, and `check_path_flags` passes it
    through untouched -- verified in circle 1 of task 5.

    What catches it is the `resolve_within` on the dry run's `output_stem`: the path the run would
    actually write, judged as a path. That closes every flag that composes a path, including ones
    not yet written, which extending `PATH_FLAGS` would not.
    """
    escape = "../" * 8 + "tmp/pwned"
    status, answer = _call(queue_server, "POST", "/api/jobs",
                           {"args": _job_args(queue_server, tag=escape), "note": ""})
    assert status == 400, answer
    assert answer["error"]["code"] == "path_outside_root", answer
    assert _pending(queue_server) == []


def test_a_tag_that_stays_inside_the_outdir_is_still_accepted(queue_server):
    """The other half: refusing every tag would satisfy the test above."""
    status, answer = _call(queue_server, "POST", "/api/jobs",
                           {"args": _job_args(queue_server, tag="ночь-15с"), "note": ""})
    assert status == 200, answer
    assert answer["job"]["output_stem"].startswith(str(queue_server.outdir))


def test_a_path_flag_outside_the_roots_never_reaches_a_subprocess(queue_server, monkeypatch):
    """Order, not just outcome: the path check runs before the dry run, so a traversal attempt
    never becomes a process. A server that validated first would spawn `h3` on an attacker's path.
    """
    spawned = []
    monkeypatch.setattr(web, "validate_args",
                        lambda *a, **k: spawned.append(a) or {"output_stem": "/x", "canvas": "1x1"})
    status, answer = _call(queue_server, "POST", "/api/jobs",
                           {"args": _job_args(queue_server, "--outdir", "/etc"), "note": ""})
    assert status == 400 and answer["error"]["code"] == "path_outside_root", answer
    assert spawned == [], "the dry-run subprocess started for a path outside every root"


def test_the_job_stores_the_resolved_paths_not_what_the_browser_sent(queue_server):
    """`resolve()` anchors a relative value at *this* process's working directory, and the worker
    runs from another one. Checking one path and queueing a different one is the bug; storing what
    was checked is the fix, which is why `check_path_flags` returns the argument list.
    """
    roundabout = f"{queue_server.outdir}/../{queue_server.outdir.name}/./sub"
    (queue_server.outdir / "sub").mkdir()
    status, answer = _call(queue_server, "POST", "/api/jobs",
                           {"args": _job_args(queue_server, "--outdir", roundabout,
                                              "--outdir", roundabout), "note": ""})
    assert status == 200, answer
    stored = answer["job"]["args"]
    assert roundabout not in stored, "the job kept the unresolved spelling the browser sent"
    assert str((queue_server.outdir / "sub").resolve()) in stored
    assert ".." not in " ".join(stored)


def test_check_path_flags_returns_the_argument_list_it_checked(tmp_path, monkeypatch):
    """Unit-level, on both spellings and both of the two ways a path can fail to be absolute.

    A `~` is the second half of the same defect as a relative path: argparse's `type=Path` does
    not expand it, so `--outdir ~/video-out` reaches the filesystem as a directory literally named
    `~`.
    """
    roots = _roots(tmp_path)
    monkeypatch.setenv("HOME", str(roots["repo"]))
    given = ["generate", "x", "--outdir", str(roots["outdir"]) + "/../outdir",
             f"--prompt-file=~/p.txt", "--tag", "a"]
    got = web.check_path_flags(given, roots)
    assert got is not given and given[3].endswith("/../outdir"), "the input must not be mutated"
    assert got[3] == str(roots["outdir"].resolve())
    assert got[4] == f"--prompt-file={(roots['repo'] / 'p.txt').resolve()}"
    assert got[:3] == ["generate", "x", "--outdir"] and got[5:] == ["--tag", "a"]


def test_the_estimate_is_stored_on_the_job(queue_server):
    """The page sums the pending jobs' estimates to answer "when will the night be over". That sum
    only exists if each job carries its own.
    """
    status, answer = _call(queue_server, "POST", "/api/jobs",
                           {"args": _job_args(queue_server, "--width", "448", "--height", "288",
                                              "--duration", "10"),
                            "note": ""})
    assert status == 200, answer
    stored = answer["job"]["estimate"]
    assert stored["forwards"] == 30
    assert stored["seconds"] > 0 and stored["peak_gb"] > 12
    assert stored == answer["estimate"]


def test_posting_a_job_with_an_image_never_pulls_mlx_into_the_server(tmp_path):
    """The one route where MLX could sneak in.

    `--image` without an explicit canvas is what makes `spec_from_args` call `resolve_canvas`,
    which imports `minimax_h3_mlx.packing` and `mlx.core` with it. Checking only that the module
    imports cleanly, or only that `/api/state` stays clean, misses it entirely -- the import
    happens on the first *submission*, inside whichever process validates it. The whole server is
    therefore run in a subprocess of its own and asked, after a real POST, what it has imported.
    """
    outdir = tmp_path / "outdir"
    (outdir / "run").mkdir(parents=True)
    repo = tmp_path / "repo"
    (repo / "prompts").mkdir(parents=True)
    models = bake_adaln_table(tmp_path / "models" / "ckpt")
    image = outdir / "frame.png"
    _write_png(image)

    script = f"""
import json, sys, threading, urllib.request
from pathlib import Path
sys.path.insert(0, {str(PROJECT_ROOT)!r})
from h3_48gb import queue as q, web

outdir = Path({str(outdir)!r})
root = q.layout(outdir / "queue")["root"]
httpd = web.make_server(root, outdir, repo=Path({str(repo)!r}),
                        models=Path({str(models.parent)!r}), webui=Path({str(tmp_path)!r}), port=0)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
body = json.dumps({{"args": ["generate", "кадр", "--image", {str(image)!r},
                             "--tag", "i", "--outdir", str(outdir),
                             "--checkpoint", {str(models)!r}], "note": ""}}).encode()
request = urllib.request.Request(f"http://127.0.0.1:{{httpd.server_address[1]}}/api/jobs",
                                 data=body, method="POST",
                                 headers={{"Content-Type": "application/json"}})
try:
    answer = json.loads(urllib.request.urlopen(request).read())
except urllib.error.HTTPError as exc:
    answer = json.loads(exc.read())
httpd.shutdown()
print(json.dumps({{"answer": answer, "mlx": sorted(n for n in sys.modules if n.split(".")[0] == "mlx")}}))
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                            cwd=str(PROJECT_ROOT), timeout=300)
    assert result.returncode == 0, f"subprocess failed: {result.stderr}"
    seen = json.loads(result.stdout.strip().splitlines()[-1])
    assert seen["answer"].get("ok") is True, (
        f"the POST never reached submission, so nothing was proven: {seen['answer']}")
    assert seen["answer"]["job"]["output_stem"].endswith("h3-i-768x768"), (
        "the canvas must have been derived from the keyframe -- that is the MLX-shaped path")
    assert seen["mlx"] == [], (
        f"the server process imported MLX while validating a job: {seen['mlx']}")


# -- Step 3: what `validate_args` actually runs ---------------------------------------------------


def test_validate_args_puts_dry_run_immediately_after_the_subcommand(tmp_path):
    """Appended at the end, a `--` in the caller's own arguments would push `--dry-run` past
    argparse's option terminator. That particular list happens to fail rather than run, but the
    property "this command line cannot become a real generation" must not rest on a corner case
    of argparse. Placed second, nothing after it can reach it.
    """
    fake = _fake_python(tmp_path, 'print(json.dumps({"dry_run": True, "output_stem": "/x",'
                                  ' "canvas": "896x512", "argv": sys.argv[1:]}))')
    report = web.validate_args(["generate", "кот", "--tag", "a", "--"], python=fake)
    assert report["argv"][:5] == ["-m", "h3_48gb", "generate", "--dry-run", "--json"]
    assert report["argv"][5:] == ["кот", "--tag", "a", "--"]


def test_a_subprocess_that_did_not_dry_run_is_refused(tmp_path):
    """The assertion that outlives an edit to `validate_args`: a report with no `dry_run: true` in
    it is not a dry run, whatever the command line said, and this server must never treat the
    output of a real generation as validation.
    """
    fake = _fake_python(tmp_path, 'print(json.dumps({"output_stem": "/x", "canvas": "896x512"}))')
    with pytest.raises(CliError) as excinfo:
        web.validate_args(["generate", "кот"], python=fake)
    assert excinfo.value.code == "internal_error"


def test_validate_args_reraises_the_clis_own_refusal_with_its_code(tmp_path):
    fake = _fake_python(tmp_path, 'print(json.dumps({"ok": False, "error": {'
                                  '"code": "prompt_missing", "message": "нет промпта",'
                                  ' "detail": {"x": 1}}}))\nsys.exit(1)')
    with pytest.raises(CliError) as excinfo:
        web.validate_args(["generate"], python=fake)
    assert excinfo.value.code == "prompt_missing"
    assert excinfo.value.detail == {"x": 1}


def test_a_refusal_naming_an_undocumented_code_becomes_args_invalid(tmp_path):
    """A code the shared contract does not list cannot be forwarded: `CliError` asserts membership,
    so forwarding it blindly would turn a refusal into an `AssertionError` and a 500.
    """
    fake = _fake_python(tmp_path, 'print(json.dumps({"ok": False, "error": {"code": "made_up",'
                                  ' "message": "?"}}))\nsys.exit(1)')
    with pytest.raises(CliError) as excinfo:
        web.validate_args(["generate"], python=fake)
    assert excinfo.value.code == "args_invalid"


def test_exit_two_is_args_invalid_with_argparses_own_stderr(tmp_path):
    fake = _fake_python(tmp_path, 'print("unrecognized arguments: --widht", file=sys.stderr)\n'
                                  'sys.exit(2)')
    with pytest.raises(CliError) as excinfo:
        web.validate_args(["generate"], python=fake)
    assert excinfo.value.code == "args_invalid"
    assert "--widht" in excinfo.value.detail["stderr"]


def test_a_dry_run_that_never_returns_is_not_a_wedged_http_thread(tmp_path):
    fake = _fake_python(tmp_path, "import time\ntime.sleep(30)")
    with pytest.raises(CliError) as excinfo:
        web.validate_args(["generate"], python=fake, timeout=0.5)
    assert excinfo.value.code == "internal_error"


def test_an_empty_argument_list_is_a_refusal_not_a_crash():
    with pytest.raises(CliError) as excinfo:
        web.validate_args([])
    assert excinfo.value.code == "args_invalid"


# -- Step 5: editing, promoting, deleting ----------------------------------------------------------


def _queue_a_job(live: _Live, *extra, tag="ночь", note=""):
    status, answer = _call(live, "POST", "/api/jobs",
                           {"args": _job_args(live, *extra, tag=tag), "note": note})
    assert status == 200, answer
    return answer["job"]


def test_editing_a_pending_job_replaces_it(queue_server):
    source = queue_server.repo / "prompts" / "scene.txt"
    source.write_text("первый", encoding="utf-8")
    job = _queue_a_job(queue_server, "--prompt-file", str(source), note="было")

    source.write_text("второй", encoding="utf-8")
    status, answer = _call(queue_server, "PUT", f"/api/jobs/{job['id']}",
                           {"args": _job_args(queue_server, "--prompt-file", str(source),
                                              "--duration", "12"),
                            "note": "стало"})
    assert status == 200, answer
    edited = answer["job"]
    assert edited["id"] == job["id"], "an edit keeps the id: the log and the snapshot hang off it"
    assert edited["note"] == "стало"
    assert "12" in edited["args"] and edited["estimate"] != job["estimate"]
    snapshot = queue_server.queue_root / "prompts" / f"{job['id']}.txt"
    assert snapshot.read_text(encoding="utf-8") == "второй"
    assert len(_pending(queue_server)) == 1


def test_editing_a_job_the_worker_took_is_a_conflict(queue_server):
    """409 rather than 400: the request was valid and lost a race. The job left `pending/` between
    the page's last poll and this click, and the only honest answer is to say so.
    """
    job = _queue_a_job(queue_server)
    assert q.claim(queue_server.queue_root).id == job["id"]

    status, answer = _call(queue_server, "PUT", f"/api/jobs/{job['id']}",
                           {"args": _job_args(queue_server, "--duration", "12"), "note": ""})
    assert status == 409, answer
    assert answer["error"]["code"] == "job_not_pending", answer


def test_posting_a_job_whose_output_name_is_taken_is_refused(queue_server):
    """The output name carries no seed, so two jobs with one tag write the same `.mp4`, `.wav`,
    `.npz` and report -- and the second silently overwrites the first.
    """
    first = _queue_a_job(queue_server, tag="одна")
    status, answer = _call(queue_server, "POST", "/api/jobs",
                           {"args": _job_args(queue_server, tag="одна"), "note": ""})
    assert status == 400, answer
    assert answer["error"]["code"] == "output_stem_conflict", answer
    assert answer["error"]["detail"]["output_stem"] == first["output_stem"], (
        "the only useful thing to say is which name is taken")
    assert len(_pending(queue_server)) == 1


def test_editing_a_job_into_a_name_another_job_holds_is_refused(queue_server):
    _queue_a_job(queue_server, tag="первая")
    second = _queue_a_job(queue_server, tag="вторая")
    status, answer = _call(queue_server, "PUT", f"/api/jobs/{second['id']}",
                           {"args": _job_args(queue_server, tag="первая"), "note": ""})
    assert status == 400 and answer["error"]["code"] == "output_stem_conflict", answer


def test_editing_a_job_without_renaming_it_is_not_a_conflict(queue_server):
    """The paired case, and the common one: changing a seed or a note leaves the output name where
    it was, and a conflict check that did not exclude the job being edited would refuse it.
    """
    job = _queue_a_job(queue_server, tag="та-же")
    status, answer = _call(queue_server, "PUT", f"/api/jobs/{job['id']}",
                           {"args": _job_args(queue_server, "--seed", "7", tag="та-же"),
                            "note": ""})
    assert status == 200, answer
    assert answer["job"]["output_stem"] == job["output_stem"]


def test_deleting_and_promoting_work_only_while_pending(queue_server):
    first = _queue_a_job(queue_server, tag="первая")
    second = _queue_a_job(queue_server, tag="вторая")

    status, answer = _call(queue_server, "POST", f"/api/jobs/{second['id']}/top")
    assert status == 200, answer
    assert answer["job"]["priority"] > first["priority"]
    state = _json(queue_server, "/api/state")[1]
    assert [row["id"] for row in state["queue"]["pending"]][0] == second["id"], (
        "the page's first job must be the job the worker takes next")

    status, answer = _call(queue_server, "DELETE", f"/api/jobs/{first['id']}")
    assert status == 200, answer
    assert [job.id for job in _pending(queue_server)] == [second["id"]]

    assert q.claim(queue_server.queue_root).id == second["id"]
    for method, url in (("POST", f"/api/jobs/{second['id']}/top"),
                        ("DELETE", f"/api/jobs/{second['id']}")):
        status, answer = _call(queue_server, method, url)
        assert status == 409, (method, answer)
        assert answer["error"]["code"] == "job_not_pending", answer


def test_deleting_a_job_removes_its_prompt_snapshot(queue_server):
    source = queue_server.repo / "prompts" / "scene.txt"
    source.write_text("текст", encoding="utf-8")
    job = _queue_a_job(queue_server, "--prompt-file", str(source))
    snapshot = queue_server.queue_root / "prompts" / f"{job['id']}.txt"
    assert snapshot.exists()
    assert _call(queue_server, "DELETE", f"/api/jobs/{job['id']}")[0] == 200
    assert not snapshot.exists()


@pytest.mark.parametrize("raw_id", ["../../../../tmp/pwned", "..%2f..%2fescape",
                                    "sub/nested", ""])
def test_a_job_id_that_is_really_a_path_is_refused(queue_server, raw_id):
    """The id arrives in a URL and becomes a filename. `cancel` unlinks what it is given, so this
    is the check standing between a URL and `rm` on an arbitrary file.
    """
    victim = queue_server.outdir / "pwned.json"
    victim.write_text("{}")
    status, answer = _call(queue_server, "DELETE", f"/api/jobs/{raw_id}")
    assert status in (400, 404), answer
    if status == 400:
        assert answer["error"]["code"] == "path_outside_root", answer
    assert victim.exists()


def test_a_job_id_that_does_not_exist_is_a_conflict_not_a_crash(queue_server):
    quoted = urllib.parse.quote("20260811-000000-нет-abcd")
    assert quoted != "20260811-000000-нет-abcd", "the id must arrive percent-encoded"
    status, answer = _call(queue_server, "DELETE", f"/api/jobs/{quoted}")
    assert status == 409 and answer["error"]["code"] == "job_not_pending", answer


# -- Step 4: /api/estimate --------------------------------------------------------------------------


def test_the_estimate_route_answers_without_starting_a_subprocess(queue_server, monkeypatch):
    """The form recomputes this on every keystroke. A subprocess per keystroke would make typing
    in a number a fork bomb; the estimate is a formula and stays one.
    """
    monkeypatch.setattr(web, "validate_args", lambda *a, **k: pytest.fail(
        "/api/estimate must not run the dry-run subprocess"))
    status, answer = _call(queue_server, "POST", "/api/estimate",
                           {"args": _job_args(queue_server, "--width", "896", "--height", "576",
                                              "--duration", "15", "--steps", "8")})
    assert status == 200, answer
    assert answer["estimate"]["forwards"] == 7
    assert 100 < answer["estimate"]["seconds"] / 60 < 200
    assert _pending(queue_server) == [], "/api/estimate must not queue anything"


def test_the_estimate_route_refuses_a_path_outside_the_roots(queue_server):
    """Without the path check this route reads `quant_config.json` from any directory a caller
    names, which turns an estimate into an existence oracle for the whole filesystem.
    """
    status, answer = _call(queue_server, "POST", "/api/estimate",
                           {"args": ["generate", "x", "--checkpoint", "/etc"]})
    assert status == 400 and answer["error"]["code"] == "path_outside_root", answer


def test_the_estimate_route_refuses_a_command_that_may_not_be_queued(queue_server):
    status, answer = _call(queue_server, "POST", "/api/estimate", {"args": ["worker"]})
    assert status == 400 and answer["error"]["code"] == "command_not_allowed", answer


# -- Step 6: prompts ---------------------------------------------------------------------------------


def test_listing_prompts_returns_names_and_sizes(queue_server):
    prompts = queue_server.repo / "prompts"
    (prompts / "b.txt").write_text("два", encoding="utf-8")
    (prompts / "a.txt").write_text("один", encoding="utf-8")
    (prompts / "notes.md").write_text("не промпт", encoding="utf-8")

    status, answer = _json(queue_server, "/api/prompts")
    assert status == 200, answer
    assert [row["name"] for row in answer["prompts"]] == ["a.txt", "b.txt"], (
        "sorted by name, and only the names another route would agree to read")
    assert answer["prompts"][0]["bytes"] == len("один".encode("utf-8"))


def test_reading_a_prompt_returns_its_text(queue_server):
    (queue_server.repo / "prompts" / "scene.txt").write_text("кот\nи пёс\n", encoding="utf-8")
    status, answer = _json(queue_server, "/api/prompts/scene.txt")
    assert status == 200, answer
    assert answer["text"] == "кот\nи пёс\n"
    assert answer["name"] == "scene.txt"


def test_saving_a_prompt_is_durable_and_lands_in_the_repository(queue_server, monkeypatch):
    """Durable because everything this server confirms is durable: temp file, fsync, replace,
    fsync the directory. Reused from the queue rather than reimplemented, and asserted by watching
    that the shared helper is the thing that ran.
    """
    calls = []
    real = q.write_text_durably
    monkeypatch.setattr(q, "write_text_durably",
                        lambda path, text: calls.append(Path(path)) or real(path, text))

    status, answer = _call(queue_server, "PUT", "/api/prompts/новый.txt".replace("новый", "new"),
                           {"text": "свежий промпт"})
    assert status == 200, answer
    target = queue_server.repo / "prompts" / "new.txt"
    assert target.read_text(encoding="utf-8") == "свежий промпт"
    assert calls == [target], "the prompt was written without the durable protocol"
    assert _json(queue_server, "/api/prompts/new.txt")[1]["text"] == "свежий промпт"


@pytest.mark.parametrize("name", ["../secret.txt", "a/b.txt", "p.py", "p", "..%2fsecret.txt"])
@pytest.mark.parametrize("method", ["GET", "PUT"])
def test_prompt_names_with_separators_or_wrong_suffix_are_refused(queue_server, name, method):
    (queue_server.repo / "secret.txt").write_text("тайна", encoding="utf-8")
    payload = {"text": "подмена"} if method == "PUT" else None
    status, answer = _call(queue_server, method, f"/api/prompts/{name}", payload)
    assert status == 400, answer
    assert answer["error"]["code"] == "prompt_name_invalid", answer
    assert (queue_server.repo / "secret.txt").read_text(encoding="utf-8") == "тайна"


def test_a_prompt_that_does_not_exist_is_a_json_404(queue_server):
    status, answer = _json(queue_server, "/api/prompts/absent.txt")
    assert status == 404 and answer["error"]["code"] == "not_found", answer


def test_saving_a_prompt_without_text_is_a_refusal(queue_server):
    status, answer = _call(queue_server, "PUT", "/api/prompts/x.txt", {"note": "нет текста"})
    assert status == 400 and answer["error"]["code"] == "args_invalid", answer


# -- the write routes are behind Host, and behind Origin as well ---------------------------------------


def test_the_host_check_covers_the_write_routes_too(queue_server):
    """Task 5 left `POST`, `PUT` and `DELETE` answering 501 from the base class, i.e. past
    `_respond` and past the `Host` check. Adding handlers beside `_respond` rather than through it
    would have opened writes to DNS rebinding, which is why every one of them is a one-line call.
    """
    for method, url in (("POST", "/api/jobs"), ("PUT", "/api/jobs/x"), ("DELETE", "/api/jobs/x"),
                        ("POST", "/api/estimate")):
        status, answer = _call(queue_server, method, url, {"args": []},
                               headers={"Host": "evil.example"})
        assert status == 403, (method, url, answer)
        assert answer["error"]["code"] == "host_not_allowed", answer


@pytest.mark.parametrize("headers", [
    {"Origin": "http://evil.example"},
    {"Origin": "https://127.0.0.1"},
    {"Origin": "null"},
    {"Sec-Fetch-Site": "cross-site"},
    {"Sec-Fetch-Site": "same-site"},
])
def test_a_write_asked_for_by_another_site_is_refused(queue_server, headers):
    """`Host` does not cover this and cannot: a cross-site form posting to
    `http://127.0.0.1:8765/api/jobs` sends exactly the right `Host`. What the browser adds, and a
    page cannot forge, is where the request came *from*.
    """
    status, answer = _call(queue_server, "POST", "/api/jobs",
                           {"args": _job_args(queue_server), "note": ""}, headers=headers)
    assert status == 403, answer
    assert answer["error"]["code"] == "origin_not_allowed", answer
    assert _pending(queue_server) == []


@pytest.mark.parametrize("headers", [
    {"Origin": "http://127.0.0.1:{port}", "Sec-Fetch-Site": "same-origin"},
    {"Origin": "http://localhost:{port}"},
    {"Sec-Fetch-Site": "none"},
    {},
])
def test_a_write_from_this_page_or_from_a_terminal_is_accepted(queue_server, headers):
    """The other half. A check that refused everything satisfies the test above; this one says the
    page still works -- and that `curl`, which sends neither header and cannot be a cross-site
    request, still works too.
    """
    sent = {key: value.format(port=queue_server.port) for key, value in headers.items()}
    status, answer = _call(queue_server, "POST", "/api/jobs",
                           {"args": _job_args(queue_server, tag=f"t{len(sent)}{len(str(sent))}"),
                            "note": ""}, headers=sent)
    assert status == 200, answer


def test_a_read_does_not_need_an_origin(queue_server):
    """Reads are not what CSRF steals -- the response never reaches the attacker's script, `Host`
    already covers rebinding, and requiring `Origin` on `GET` would break every ordinary
    navigation, `<img>` and `<video>` on the page itself.
    """
    status, _ = _json(queue_server, "/api/state")
    assert status == 200


@pytest.mark.parametrize("method,url", [("POST", "/api/nope"), ("PUT", "/api/nope"),
                                        ("DELETE", "/api/nope"), ("POST", "/api/jobs/x/nope")])
def test_a_write_method_on_an_unknown_route_is_a_json_404(queue_server, method, url):
    status, answer = _call(queue_server, method, url, {"args": []})
    assert status == 404 and answer["error"]["code"] == "not_found", answer


# -- request bodies -------------------------------------------------------------------------------


def test_a_body_that_is_not_json_is_a_400_not_a_500(queue_server):
    connection = http.client.HTTPConnection(web.LOOPBACK, queue_server.port, timeout=30)
    try:
        connection.request("POST", "/api/jobs", body=b"{not json",
                           headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        answer = json.loads(response.read())
        assert response.status == 400, answer
        assert answer["error"]["code"] == "bad_request", answer
    finally:
        connection.close()


@pytest.mark.parametrize("payload", [{}, {"args": "generate"}, {"args": [1, 2]},
                                     {"args": ["generate"], "note": 5}])
def test_a_body_without_a_usable_args_list_is_args_invalid(queue_server, payload):
    status, answer = _call(queue_server, "POST", "/api/jobs", payload)
    assert status == 400, answer
    assert answer["error"]["code"] in ("args_invalid", "command_not_allowed"), answer


def test_a_body_longer_than_the_limit_is_refused(queue_server):
    """The `Content-Length` is a promise, and the bytes are never sent.

    The short timeout is the point of the test as much as the assertion is: with the limit
    removed, `rfile.read` blocks for bytes that will never arrive, and a test that hangs reports
    nothing at all (task 5 lost a whole mutation run to exactly this). Five seconds turns the
    missing check into a failure instead of a wedge.
    """
    connection = http.client.HTTPConnection(web.LOOPBACK, queue_server.port, timeout=5)
    try:
        connection.putrequest("POST", "/api/jobs")
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", str(web.MAX_BODY_BYTES + 1))
        connection.endheaders()
        response = connection.getresponse()
        answer = json.loads(response.read())
        assert response.status == 400, answer
        assert answer["error"]["code"] == "bad_request", answer
    finally:
        connection.close()


# -- the contract ------------------------------------------------------------------------------------


def test_the_four_new_codes_are_in_the_shared_contract():
    from h3_48gb.cli import ERROR_CODES

    assert {"args_invalid", "command_not_allowed", "output_stem_conflict",
            "job_not_pending"} <= set(ERROR_CODES)


def test_the_planned_code_exemption_is_now_empty():
    """`job_not_pending` was mapped to 409 a task before it could be raised. It is raised now, so
    the exemption has done its job and is gone -- and `test_every_status_this_module_maps_belongs_
    to_a_real_code` covers the entry under the ordinary rule again.
    """
    assert web.PLANNED_CODES == frozenset()
    assert web.ERROR_STATUS["job_not_pending"] == 409


def test_a_job_that_does_not_name_an_outdir_at_all_is_refused(queue_server):
    """`--outdir` left out means argparse's default, `H3_OUTDIR` or `~/video-out` -- a directory
    the running server was never pointed at. No token exists for `check_path_flags` to inspect, so
    the only thing standing here is the `output_stem` check, judging the path the run would write.
    """
    args = ["generate", "кот", "--checkpoint", str(queue_server.models / "ckpt"), "--tag", "нет"]
    status, answer = _call(queue_server, "POST", "/api/jobs", {"args": args, "note": ""})
    assert status == 400, answer
    assert answer["error"]["code"] == "path_outside_root", answer
    assert _pending(queue_server) == []


def test_the_estimate_route_reads_the_bit_width_through_the_resolved_checkpoint(queue_server,
                                                                                monkeypatch):
    """One request must not be quoted at 4 bits here and 8 bits on submission. `--checkpoint
    ~/models/...` is the case: without normalisation `quant_bits` opens a directory literally
    named `~`, finds nothing, and falls back to four.
    """
    monkeypatch.setenv("HOME", str(queue_server.models.parent))
    (queue_server.models / "ckpt" / "quant_config.json").write_text(json.dumps({"bits": 8}))
    status, answer = _call(queue_server, "POST", "/api/estimate",
                           {"args": ["generate", "кот", "--checkpoint", "~/models/ckpt"]})
    assert status == 200, answer
    assert answer["estimate"]["bits"] == 8, answer


def test_two_browsers_posting_the_same_tag_at_once_produce_one_job(queue_server):
    """A race checked by racing, as the design spec requires -- two real threads, not "submit,
    then submit again".

    The output name carries no seed, so two jobs under one tag write the same `.mp4`. The queue's
    exclusive lock is what makes the check-and-write one step; from up here the only visible proof
    is that exactly one of two simultaneous submissions wins and the other is told which name is
    taken.
    """
    results = []
    barrier = threading.Barrier(2)

    def post(seed):
        barrier.wait(timeout=30)
        results.append(_call(queue_server, "POST", "/api/jobs",
                             {"args": _job_args(queue_server, "--seed", str(seed), tag="одна"),
                              "note": ""}))

    threads = [threading.Thread(target=post, args=(seed,)) for seed in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=180)
        assert not thread.is_alive(), "a submission never finished"

    codes = sorted(status for status, _ in results)
    assert codes == [200, 400], results
    refused = next(answer for status, answer in results if status == 400)
    assert refused["error"]["code"] == "output_stem_conflict", refused
    assert len(_pending(queue_server)) == 1


# -- Step 7: the page -----------------------------------------------------------------------------

WEBUI = PROJECT_ROOT / "h3_48gb" / "webui"
PAGE_FILES = ("index.html", "style.css", "app.js")

#: A `data:` URI is a literal, not a request. The stylesheet draws the select chevron as inline
#: SVG, and inline SVG carries the XML namespace `http://www.w3.org/2000/svg` -- a name, never
#: fetched. Stripping the URIs before looking for external addresses is what keeps the check from
#: flagging the very technique that makes the page self-contained.
_DATA_URI = re.compile(r"""url\(\s*["']?data:[^)]*\)""", re.IGNORECASE)
#: A host name, not any two slashes: the first version matched `//` followed by a letter, which
#: is every `//TODO` and every `// note` in the script. See
#: `test_the_external_address_detector_knows_a_host_from_a_comment` for both sides of the line.
_EXTERNAL = re.compile(r"""(?:\bhttps?:)?//[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+""")


def _page_text(name: str) -> str:
    return (WEBUI / name).read_text(encoding="utf-8")


def test_the_page_is_exactly_three_files_with_the_names_the_server_serves():
    """`index.html` names the other two by URL, so a rename that missed one would ship a page
    with no stylesheet and no behaviour, and every other test here would still pass.
    """
    assert sorted(p.name for p in WEBUI.iterdir() if p.is_file()) == sorted(PAGE_FILES)


def test_index_is_served_and_references_only_local_assets(server):
    """The page arrives at `/`, and every address in it is this server's own.

    Both halves matter. A page that loads is not enough -- one `<link>` to a CDN and the whole
    thing stops working on the machine it was written for, which has no route to the internet
    while a generation is running and may have none at all in five years.
    """
    status, headers, body = _request(server, "/")
    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    page = body.decode("utf-8")

    referenced = re.findall(r"""(?:href|src)=["']([^"']+)["']""", page)
    assert referenced, "the page references neither a stylesheet nor a script"
    for url in referenced:
        assert url.startswith("/static/"), f"{url} is not served by this server"
        assert _request(server, url)[0] == 200, f"{url} is referenced but not served"

    assert "/static/style.css" in referenced
    assert "/static/app.js" in referenced


@pytest.mark.parametrize("name", PAGE_FILES)
def test_no_page_file_names_an_address_off_this_machine(name):
    """No font, no library, no image from the network -- in any of the three files.

    Checked on the bytes rather than on a rendered page: a `fetch("https://...")` inside a branch
    nobody took would never show up in a browser test, and it is exactly as fatal.
    """
    text = _DATA_URI.sub("", _page_text(name))
    found = _EXTERNAL.findall(text)
    assert not found, f"{name} names an external address: {found}"


def test_the_page_follows_the_system_theme_and_stops_moving_when_asked():
    """Two media queries, both required by the brief and neither visible to any other test here.

    Presentation only, so no mutation was run against this one -- it is a spelling check on the
    stylesheet, not a claim about behaviour.
    """
    css = _page_text("style.css")
    assert "@media (prefers-color-scheme: dark)" in css
    assert "@media (prefers-reduced-motion: reduce)" in css


def test_the_page_asks_for_its_own_routes_in_a_way_the_provenance_check_accepts():
    """Every request the page makes is same-origin and left alone for the browser to stamp.

    The server refuses a write whose `Origin` or `Sec-Fetch-Site` names another site. A browser
    fills both in correctly by itself; the ways to break that are all things the page would have
    to *add* -- an absolute URL to another host, `mode: "no-cors"` (which strips the body and the
    headers), `credentials: "omit"`, or a hand-written `Origin`. None of them appear, and a future
    edit that adds one fails here rather than at three in the morning with a 403.
    """
    script = _page_text("app.js")
    urls = re.findall(r"""(?:fetch|api)\(\s*(?:"[A-Z]+",\s*)?["'`]([^"'`$]*)""", script)
    assert urls, "no request in the page at all"
    for url in urls:
        assert url.startswith("/"), f"{url!r} is not a same-origin path"
    assert '"/api/state"' in script, "the page has to poll the one route it redraws from"
    for forbidden in ('no-cors', 'credentials: "omit"', '"Origin"'):
        assert forbidden not in script, f"{forbidden} would break the server's provenance check"


# -- Step 7: the page's behaviour, without a browser ----------------------------------------------

_NODE = shutil.which("node")
_needs_node = pytest.mark.skipif(
    _NODE is None,
    reason="`node` is not in PATH; the page's pure functions cannot be called outside a browser, "
           "so requirements 2-10 would have to be checked by eye instead")


def _node_eval(body: str, timeout: float = 60):
    """Run `body` as an ES module with the page's own `app.js` imported, and parse what it prints.

    The real file is imported -- not a copy, not a re-implementation -- which is the only reason
    a green test here says anything about the page a browser gets. `app.js` guards its DOM half
    behind `typeof document`, so importing it outside a browser wires nothing up.
    """
    source = f'import * as app from {json.dumps((WEBUI / "app.js").as_uri())};\n{body}'
    result = subprocess.run([_NODE, "--input-type=module", "-e", source],
                            capture_output=True, text=True, timeout=timeout)
    assert result.returncode == 0, f"node refused the module:\n{result.stderr}"
    return json.loads(result.stdout)


@_needs_node
def test_the_page_module_imports_outside_a_browser():
    """The precondition of every test below it: if this fails they all pass vacuously by never
    running, so it is asserted separately and first.
    """
    names = _node_eval("console.log(JSON.stringify(Object.keys(app).sort()));")
    assert "analysePrompt" in names and "pendingSummary" in names


@_needs_node
def test_the_poll_interval_is_the_twenty_seconds_the_spec_asks_for():
    assert _node_eval("console.log(JSON.stringify(app.POLL_MS));") == 20000


@_needs_node
def test_a_server_that_stops_answering_is_said_so_in_words():
    """Requirement 1. The failure mode this guards against is not a blank page -- it is a page
    that keeps showing the numbers from twenty seconds ago as if they were current.
    """
    quiet, talking = _node_eval("""
      const at = new Date("2026-08-12T21:00:00");
      const later = new Date("2026-08-12T21:02:00");
      console.log(JSON.stringify([app.offlineNotice(1, at, later),
                                  app.offlineNotice(0, at, later)]));
    """)
    assert talking is None, "a page whose last poll succeeded must say nothing"
    assert quiet is not None, "a page whose last poll failed must not go on showing stale numbers"
    assert "не отвечает" in quiet
    assert "2 мин" in quiet, quiet


@_needs_node
def test_the_form_keeps_its_values_and_moves_on_to_the_next_seed_and_tag():
    """Requirement 2. The tag carries the seed because the output name is built from the tag:
    without it the second seed of one scene is refused as `output_stem_conflict`.
    """
    first, second = _node_eval("""
      const a = app.advanceAfterSubmit({seed: 7, tag: "centaur-15s"});
      const b = app.advanceAfterSubmit(a);
      console.log(JSON.stringify([a, b]));
    """)
    assert first == {"seed": 8, "tag": "centaur-15s-s8"}
    assert second == {"seed": 9, "tag": "centaur-15s-s9"}, "the seed must not accumulate in the tag"


@_needs_node
def test_there_are_exactly_three_canvas_presets_draft_small_large():
    presets = _node_eval("console.log(JSON.stringify(app.CANVAS_PRESETS));")
    assert [(p["key"], p["w"], p["h"]) for p in presets] == [
        ("draft", 448, 288), ("small", 896, 576), ("large", 1344, 768),
    ]


@_needs_node
def test_applying_the_draft_preset_sets_448_by_288():
    got = _node_eval('console.log(JSON.stringify(app.applyCanvasPreset("draft")));')
    assert got == {"width": 448, "height": 288}


@_needs_node
def test_an_unknown_preset_key_is_not_silently_accepted():
    got = _node_eval('console.log(JSON.stringify(app.applyCanvasPreset("huge")));')
    assert got is None


@_needs_node
def test_forty_gigabytes_warns_and_forty_six_demands_the_checkbox():
    """Requirement 4. Three outcomes, and the middle one is the one a two-branch implementation
    loses: above 40 the page must speak without blocking.
    """
    low, warn, block = _node_eval("""
      console.log(JSON.stringify([35.7, 42.0, 47.2].map((gb) => app.memoryVerdict(gb))));
    """)
    assert (low["level"], low["needsConfirm"]) == ("ok", False)
    assert (warn["level"], warn["needsConfirm"]) == ("warn", False)
    assert warn["text"], "a warning nobody can read is not a warning"
    assert (block["level"], block["needsConfirm"]) == ("block", True)


@_needs_node
def test_the_button_does_not_work_above_forty_six_until_the_checkbox_is_ticked():
    """Requirement 4, the other half: the checkbox has to actually gate the button."""
    without, with_tick, warn_only = _node_eval("""
      const block = app.memoryVerdict(47.2), warn = app.memoryVerdict(42);
      console.log(JSON.stringify([
        app.submitAllowed({verdict: block, forced: false, canvasOk: true, busy: false}),
        app.submitAllowed({verdict: block, forced: true,  canvasOk: true, busy: false}),
        app.submitAllowed({verdict: warn,  forced: false, canvasOk: true, busy: false}),
      ]));
    """)
    assert without is False
    assert with_tick is True
    assert warn_only is True, "a warning must not block the button; only 46 does"


#: A whole prompt in the documented format, written out so that the tests below can break one
#: thing at a time against a body that is otherwise beyond reproach. Four shots, two speakers,
#: two languages, both sound fields -- and, in the first line, the keyframe instruction whose
#: `(from [Shot 1])` is a *reference* to a shot rather than a fifth shot.
_GOOD_PROMPT = """\
For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is \
fully referenced.

integrated_multimodal_description: [Shot 1] Live-action, cinematic, a wide shot frames two \
figures on a ridge. The bearded man with a low, hoarse voice (S1) raises a bronze sword. \
[Shot 2] At 00:02.500, the shot cuts to a medium tracking shot as the woman with a clear, \
carrying voice (S2) shouts, <d>[Russian] Сдавайся!</d> [Shot 3] At 00:05.000, the camera cuts \
to a low-angle shot; the man (S1) answers, <d>[English] Never!</d> [Shot 4] At 00:07.000, the \
shot cuts to a close action shot as the spear clashes against the raised sword.

overall_soundscape: Gusting mountain wind carries across the open ridge throughout. Bare feet \
scrape on gravel and bronze rings against wood at the clash.

non_diegetic_music: Orchestral score at a fast tempo, opening with low war drums on a steady \
pulse. The music ends on a single loud hit.
"""

#: The same scene in the format this project invented before the guides were found: a duration
#: header, a `Characters:` block, and `[0.0-2.5s]` shots. Every one of those is markup the model
#: has no field for, and the parse has to say so rather than add them up.
_OLD_FORMAT_PROMPT = """\
[10s, multi-shot dynamic action sequence] Live-action, cinematic realism, a rocky ridge.

Characters: A warrior man (M1) with a bronze sword. A warrior woman (W1) with a spear.

[0.0-2.5s] WIDE SHOT: he slashes, she dodges.
[2.5-10.0s] CLOSE ACTION SHOT: the spear clashes against the sword.

overall_soundscape: Bronze on wood, gravel underfoot, gusting wind.

non_diegetic_music: A driving epic orchestral battle theme with pounding war drums.
"""


def _parse(prompt: str, seconds: float = 10, audio: bool = True):
    """`[(kind, text)]` for one prompt, with the markup inside each note stripped.

    The notes carry `<span class="num">` and escaped `&lt;d&gt;`, neither of which is what the
    test is about; what a human reads is the text, so that is what is asserted on.
    """
    notes = _node_eval("""
      const a = app.analysePrompt(%s, %s, {audio: %s});
      console.log(JSON.stringify(a.notes.map(n => [n.k, n.t])));
    """ % (json.dumps(prompt), seconds, "true" if audio else "false"))
    unescape = (lambda t: re.sub(r"<[^>]+>", "", t)
                .replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&"))
    return [(kind, unescape(text)) for kind, text in notes]


def _flat(parsed) -> str:
    return " | ".join(f"{k}:{t}" for k, t in parsed)


@_needs_node
def test_a_prompt_in_the_documented_format_draws_not_one_remark():
    """The other side of requirement 5, and the first thing that has to hold after the parse was
    rewritten: a parse that only ever complains is not a parse, and a parse that complains about
    a correct prompt is worse than none -- it teaches the human to stop reading the list.

    Both halves of the format's own example are here: the instruction line above the fields, and
    four shots whose cut times rise inside the ten seconds the form declares.
    """
    parsed = _parse(_GOOD_PROMPT, 10)
    assert [k for k, _ in parsed] == ["ok"] * 6, _flat(parsed)
    assert "Планов 4" in _flat(parsed), (
        "`(from [Shot 1])` in the instruction line is a reference, not a fifth shot")


@_needs_node
def test_the_official_format_prompt_in_this_repository_parses_clean():
    """The same claim against a file nobody wrote for a test.

    `prompts/tango-dancers-official.txt` was rewritten from the guides by hand; if the parse and
    the guides ever drift apart, one of the two is wrong and this is where it shows.
    """
    text = (PROJECT_ROOT / "prompts" / "tango-dancers-official.txt").read_text(encoding="utf-8")
    parsed = _parse(text, 5)
    assert [k for k, _ in parsed] == ["ok"] * 5, _flat(parsed)


@_needs_node
def test_the_prompt_the_project_used_to_write_is_called_out_as_the_wrong_format():
    """Requirement 5, re-aimed. The old prompts are still in `prompts/`, and opening one has to
    say what is wrong with it rather than silently adding up blocks the model never reads.
    """
    parsed = _parse(_OLD_FORMAT_PROMPT, 10)
    flat = _flat(parsed)
    assert any(k == "bad" and "integrated_multimodal_description" in t for k, t in parsed), flat
    assert any(k == "warn" and "[Shot N] не найдено" in t for k, t in parsed), flat
    stale = [t for k, t in parsed if "старого формата" in t]
    assert stale, flat
    for leftover in ("[10s,", "Characters:", "[0.0-2.5s]"):
        assert leftover in stale[0], (leftover, stale[0])


@_needs_node
@pytest.mark.parametrize("was,becomes,expect,kind", [
    # The first shot is the one the format spells without a time; a time on it is the mistake the
    # old format made by construction.
    ("[Shot 1] Live-action", "[Shot 1] At 00:00.000, live-action",
     "у первого плана есть метка времени", "bad"),
    # Strictly increasing, and each inside the declared duration -- both, not either.
    ("[Shot 3] At 00:05.000", "[Shot 3] At 00:02.000", "не позже предыдущей", "bad"),
    ("[Shot 4] At 00:07.000", "[Shot 4] At 00:11.000", "за пределами", "bad"),
    # A cut exactly at the end cuts to nothing: ten seconds of video end at 00:10.000.
    ("[Shot 4] At 00:07.000", "[Shot 4] At 00:10.000", "за пределами", "bad"),
    # A later shot with no time has nowhere to cut to.
    ("[Shot 2] At 00:02.500,", "[Shot 2]", "нет метки", "bad"),
    # Numbered by hand, and one lost in an edit.
    ("[Shot 3] At 00:05.000", "[Shot 5] At 00:05.000", "не подряд", "bad"),
])
def test_every_way_a_shot_line_can_break_the_format_is_named(was, becomes, expect, kind):
    """Each of these passes on a parser that only checks the others, which is why they are
    separate rows rather than one prompt carrying all six at once.
    """
    prompt = _GOOD_PROMPT.replace(was, becomes)
    assert prompt != _GOOD_PROMPT, f"{was!r} is no longer in the prompt these rows edit"
    parsed = _parse(prompt, 10)
    assert any(k == kind and expect in t for k, t in parsed), _flat(parsed)


@_needs_node
def test_speech_tags_have_to_close_and_name_a_language_the_model_speaks():
    """Three ways one `<d>` goes wrong, and the same prompt with none of them.

    The language list is the model's, not ours: eleven names, and a twelfth is not a typo the
    page may quietly accept -- the line would come out in whatever the model guessed instead.
    """
    unclosed = _parse(_GOOD_PROMPT.replace("Never!</d>", "Never!"), 10)
    assert any(k == "bad" and "не парные" in t for k, t in unclosed), _flat(unclosed)

    nested = _parse(_GOOD_PROMPT.replace("<d>[English] Never!</d>",
                                         "<d>[English] <d>Never!</d></d>"), 10)
    assert any(k == "bad" and "не по порядку" in t for k, t in nested), _flat(nested)

    foreign = _parse(_GOOD_PROMPT.replace("[English] Never!", "[Klingon] Never!"), 10)
    assert any(k == "bad" and "Klingon" in t for k, t in foreign), _flat(foreign)

    nameless = _parse(_GOOD_PROMPT.replace("<d>[English] Never!", "<d>Never!"), 10)
    assert any(k == "bad" and "язык не назван" in t for k, t in nameless), _flat(nameless)

    good = _parse(_GOOD_PROMPT, 10)
    assert any(k == "ok" and "Реплик 2" in t and "Russian" in t and "English" in t
               for k, t in good), _flat(good)


@_needs_node
def test_an_identifier_on_a_character_who_never_speaks_is_a_remark():
    """`(S1)` is the model's handle for a *voice*; the guide gives one only to a character who
    speaks, sings, or is heard off-screen. An ID with no line behind it is either a silent
    character numbered by mistake or a line that fell out of an edit, and both are worth saying.

    A remark, not a refusal: the rule is the guide's, and the prompt still runs.
    """
    silenced = _parse(_GOOD_PROMPT.replace("<d>[English] Never!</d>", "nothing at all"), 10)
    assert any(k == "warn" and "(S1)" in t and "без единой реплики" in t
               for k, t in silenced), _flat(silenced)
    assert not any(k == "warn" and "(S2)" in t for k, t in silenced), (
        "S2 still speaks; only the silent one is named")


@_needs_node
@pytest.mark.parametrize("field,over,under", [
    ("overall_soundscape", 5, 4),
    ("non_diegetic_music", 4, 3),
])
def test_each_sound_field_is_held_to_the_sentence_budget_the_guide_gives_it(field, over, under):
    """Four sentences for the soundscape and three for the music -- different numbers, so a
    parser holding both to one of them passes half of this and fails the other half.
    """
    line = f"{field}: " + " ".join(f"Sentence number {i}." for i in range(over))
    prompt = re.sub(rf"^{field}:.*?(?=\n\n|\Z)", line, _GOOD_PROMPT,
                    flags=re.MULTILINE | re.DOTALL)
    assert line in prompt
    parsed = _parse(prompt, 10)
    assert any(k == "warn" and field in t and f"{over} предлож" in t
               for k, t in parsed), _flat(parsed)

    fits = _parse(prompt.replace(line, " ".join(line.split(" ")[:-3])), 10)
    assert any(k == "ok" and field in t for k, t in fits), _flat(fits)


@_needs_node
def test_a_line_of_speech_copied_into_the_soundscape_is_a_remark():
    """The guide says it in as many words: dialogue and singing already live in the multimodal
    description and are not repeated in `overall_soundscape`. Our old prompts listed the shouts
    there, so this is the mistake this project is most likely to make again.
    """
    parsed = _parse(_GOOD_PROMPT.replace("overall_soundscape: Gusting",
                                         "overall_soundscape: <d>[Russian] Сдавайся!</d> Gusting"),
                    10)
    assert any(k == "warn" and "overall_soundscape" in t and "<d>" in t
               for k, t in parsed), _flat(parsed)


@_needs_node
def test_a_music_field_that_says_there_is_no_music_is_accepted():
    """`N/A` is what the guide writes when there is no non-diegetic score, and a sentence count
    applied to it would turn the documented spelling into a complaint.
    """
    prompt = re.sub(r"^non_diegetic_music:.*?(?=\n\n|\Z)", "non_diegetic_music: N/A",
                    _GOOD_PROMPT, flags=re.MULTILINE | re.DOTALL)
    parsed = _parse(prompt, 10)
    assert [k for k, _ in parsed] == ["ok"] * 6, _flat(parsed)
    assert any(k == "ok" and "non_diegetic_music" in t and "N/A" in t for k, t in parsed), (
        "the note has to say the field was read as N/A, not that one sentence was counted")


@_needs_node
def test_a_decimal_number_inside_a_sound_field_is_not_a_full_stop():
    """Sentences are counted by the stops between them, and `4.5` is not one. Three sentences
    counted as four is `non_diegetic_music` complaining about a field the guide would accept.
    """
    counted = _node_eval("""
      console.log(JSON.stringify(["One. Two. Three at 4.5 metres per second.",
                                  "Only one sentence.", "", "N/A"].map(app.countSentences)));
    """)
    assert counted == [3, 1, 0, 1], counted


@_needs_node
def test_mood_words_in_the_music_field_are_named_and_flagged():
    """The guide forbids them outright: instrumentation, tempo, rhythm and dynamics are what
    `non_diegetic_music` is for. "A driving epic ... tense and fast-paced" is verbatim what this
    project used to write -- difference #14 of PROMPT-FORMAT-PLAN.md -- so the page has to name
    the words it objects to, not just disapprove of the field.
    """
    line = "non_diegetic_music: A driving epic orchestral score, tense and fast-paced."
    prompt = re.sub(r"^non_diegetic_music:.*?(?=\n\n|\Z)", line, _GOOD_PROMPT,
                    flags=re.MULTILINE | re.DOTALL)
    assert line in prompt
    parsed = _parse(prompt, 10)
    note = next((t for k, t in parsed if k == "warn" and "non_diegetic_music" in t), None)
    assert note is not None, _flat(parsed)
    for word in ("epic", "tense", "fast-paced"):
        assert word in note, note

    # The guide's own good example is instruments, tempo and dynamics -- it has to pass clean.
    fits = _parse(prompt.replace(line, "non_diegetic_music: Sparse piano notes at a slow tempo, "
                                       "joined by sustained low strings that gradually increase "
                                       "in volume before fading out."), 10)
    assert any(k == "ok" and "non_diegetic_music" in t for k, t in fits), _flat(fits)


@_needs_node
def test_the_bar_under_the_editor_holds_one_segment_per_shot():
    """The shots partition the video: shot N runs from its own cut to the next one, and the last
    one to the end. Drawn only from a timeline that holds together -- on a prompt whose times go
    backwards the note above says so, and a drawing of nonsense would say it worse.
    """
    good, broken = _node_eval("""
      const good = app.analysePrompt(%s, 10, {audio: true});
      const broken = app.analysePrompt(%s, 10, {audio: true});
      console.log(JSON.stringify([good.timeline, broken.timeline]));
    """ % (json.dumps(_GOOD_PROMPT),
           json.dumps(_GOOD_PROMPT.replace("[Shot 3] At 00:05.000", "[Shot 3] At 00:02.000"))))
    assert [(seg["a"], seg["b"]) for seg in good] == [(0, 2.5), (2.5, 5), (5, 7), (7, 10)]
    assert broken == [], "a timeline that does not hold together is not drawn at all"


@_needs_node
def test_the_prompt_parse_rewrites_nothing():
    """Requirement 5's second sentence, unchanged by the new format. The highlight layer is the
    only thing the parse produces that a human could mistake for the prompt itself, so it is what
    gets compared back -- and it now has to survive `<d>`, `</d>` and `&` all at once.
    """
    same = _node_eval("""
      const prompt = %s;
      const html = app.highlightHtml(prompt, app.analysePrompt(prompt, 10, {audio: true}));
      const back = html.replace(/<\\/?mark[^>]*>/g, "")
        .replace(/&lt;/g, "<").replace(/&gt;/g, ">")
        .replace(/&quot;/g, '"').replace(/&amp;/g, "&");
      console.log(JSON.stringify(back === prompt + "\\n"));
    """ % json.dumps(_GOOD_PROMPT + "\nA & B <tag>\n"))
    assert same is True


@_needs_node
def test_only_a_waiting_job_carries_edit_top_and_delete():
    """Requirement 6, refined by task 7's duplicate button and task 8's chat: not grey buttons --
    no buttons, a grey button promises it will work one day. That still holds for
    edit/top/delete/chat, which only ever make sense for a job still waiting to run (the chat ends
    in a `PUT /api/jobs/<id>`, and that route refuses a job the worker has already claimed) -- but
    duplicate reads a job without changing it, so it is offered on the finished row too (see
    `finishedRowHtml`'s own docstring).
    """
    waiting, finished = _node_eval("""
      const job = {id: "j1", note: "ночная", priority: 0,
                   args: ["generate", "--tag", "кот", "--mode", "t2va"],
                   estimate: {seconds: 3600, peak_gb: 35, width: 896, height: 576,
                              duration_seconds: 10, steps: 8},
                   output_stem: "/o/night/h3-кот-896x576", exit_code: 0,
                   started_at: "2026-08-12T01:00:00", finished_at: "2026-08-12T02:00:00"};
      console.log(JSON.stringify([app.pendingRowHtml(job), app.finishedRowHtml(job)]));
    """)
    assert sorted(re.findall(r'data-act="([a-z]+)"', waiting)) == [
        "chat", "del", "dup", "edit", "top"]
    assert re.findall(r'data-act="([a-z]+)"', finished) == ["dup"], (
        "a finished run offers nothing but a copy of itself")


@_needs_node
def test_the_finished_rows_copy_button_has_an_explicit_place_in_the_row_grid():
    """Task 7 fix round 1. `.r` is one CSS grid (`--cols` in `style.css`) sized for exactly as
    many columns as `pendingRowHtml` has direct children -- that is the whole point of one shared
    grid across three different row shapes (see `--cols`'s own comment). `finishedRowHtml`'s
    `<span class="acts">` is always one child *past* that count (two, on a failed row, next to
    `.why`), so without an explicit `grid-column`/`grid-row` in the stylesheet it silently falls
    into the next implicit row's first (16px, meant for the status dot) column and its text gets
    clipped -- exactly the "опия" bug a browser render caught.

    This is checked from both ends, neither of them the literal number 10: the column count comes
    from parsing `--cols` itself, and the child count is measured on the actual HTML the row
    functions produce, not asserted as a constant.
    """
    css = _page_text("style.css")
    cols_match = re.search(r"--cols:\s*([^;]+);", css)
    assert cols_match, "style.css must still define --cols for the row grid"
    # A plain `.split()` would cut `minmax(180px, 1fr)` in two at its internal comma-space --
    # `minmax(...)` is kept as one token the same way a naive split would wrongly break it apart.
    column_count = len(re.findall(r"minmax\([^)]*\)|\S+", cols_match.group(1)))

    ok_children, failed_children = _node_eval("""
      // Counts *direct* children of the single outer <div>: a depth-aware tag scan, because the
      // row's own children (.name, the memory gauge, ...) nest further tags inside themselves.
      function directChildCount(rowHtml) {
        const inner = rowHtml.replace(/^<div[^>]*>/, "").replace(/<\\/div>$/, "");
        const tagRe = /<(\\/?)([a-zA-Z][a-zA-Z0-9]*)\\b[^>]*?(\\/)?>/g;
        let depth = 0, count = 0, m;
        while ((m = tagRe.exec(inner))) {
          const closing = m[1], selfClosing = m[3];
          if (selfClosing) continue;
          if (closing) { depth -= 1; }
          else { if (depth === 0) count += 1; depth += 1; }
        }
        return count;
      }
      const base = {id: "j", note: "", args: ["generate", "--tag", "кот"],
                    estimate: {width: 896, height: 576, duration_seconds: 10, steps: 8},
                    output_stem: "/o/n/h3-кот-896x576",
                    started_at: "2026-08-12T01:00:00", finished_at: "2026-08-12T02:00:00"};
      const ok = app.finishedRowHtml({...base, exit_code: 0});
      const failed = app.finishedRowHtml({...base, exit_code: 1, log_tail: "boom"});
      console.log(JSON.stringify([directChildCount(ok), directChildCount(failed)]));
    """)
    assert ok_children == column_count + 1, (
        f"a finished/succeeded row must have exactly one child ({{.acts}}) past the grid's own "
        f"{column_count} columns -- got {ok_children}; the count this test derives its "
        f"expectation from, and the row's own shape, must have drifted apart")
    assert failed_children == column_count + 2, (
        f"a failed row adds .why on top of that -- got {failed_children}")

    # The structural overflow above is real and expected (that many children never fit one row);
    # what must not be true is that the browser is left to place the overflow on its own.
    #
    # The *presence* of a `grid-column` is not the property this test is about, and asserting only
    # that let the bug back in: `grid-column: 1 / -1` is present, declared, explicit -- and puts
    # the button back in the 16px marker column, which is exactly the clipping («опия») the rule
    # exists to prevent. The value has to be checked, and the value it has to be is derived the
    # same way the column count above is, from `--cols` itself.
    tracks = re.findall(r"minmax\([^)]*\)|\S+", cols_match.group(1))
    assert re.fullmatch(r"\d+px", tracks[0]), (
        f"the grid's first track is the status marker's; it is {tracks[0]!r}, so the "
        "«one column past the marker» this test computes below no longer means that")
    marker_tracks = 1
    first_content_column = str(marker_tracks + 1)
    for state, what in (("done", "the succeeded row"), ("fail", "the failed row")):
        rule = re.search(r"\.r\.%s\s+\.acts\s*\{([^}]*)\}" % state, css)
        assert rule, (f"{what}'s .acts needs an explicit place in style.css, or it wraps into the "
                      "next implicit row's marker column and gets clipped")
        declared = re.search(r"grid-column:\s*([^;}]+)", rule.group(1))
        assert declared, f"{what}'s .acts rule has no grid-column: {rule.group(1)!r}"
        start = declared.group(1).split("/")[0].strip()
        assert start == first_content_column, (
            f"{what}'s .acts starts at column {start}, and the first column past the marker is "
            f"{first_content_column}: a button placed on the marker's own track is the clipping "
            "this rule exists to prevent, whether or not a grid-column was written down")


@_needs_node
def test_the_waiting_summary_counts_the_jobs_the_hours_and_the_hour_it_ends():
    """Requirement 7 -- the answer to the question the night queue is assembled to ask."""
    text, seconds, count = _node_eval("""
      const job = (s) => ({estimate: {seconds: s}});
      const s = app.pendingSummary([job(3600), job(5400), job(1800)],
        {now: new Date("2026-08-12T22:00:00"), runningSeconds: 0, workerState: "alive"});
      console.log(JSON.stringify([s.text, s.seconds, s.count]));
    """)
    assert count == 3 and seconds == 10800
    assert "3 задачи" in text and "3 ч" in text and "до 01:00" in text, text


@_needs_node
def test_the_summary_refuses_to_name_an_end_time_while_the_queue_stands_still():
    """The same requirement, in the state that makes an end time a lie: nothing is moving."""
    text = _node_eval("""
      const s = app.pendingSummary([{estimate: {seconds: 3600}}],
        {now: new Date("2026-08-12T22:00:00"), workerState: "stopped"});
      console.log(JSON.stringify([s.text, s.endsAt]));
    """)[0]
    assert "очередь стоит" in text and "до " not in text, text


@_needs_node
def test_progress_is_drawn_as_one_division_per_forward():
    """Requirement 8. Seven forwards, seven divisions -- "three of seven" reads faster than 43 %,
    and a percentage bar would pass a test that only counted pixels.
    """
    html = _node_eval("console.log(JSON.stringify(app.stepsHtml(3, 7)));")
    assert html.count("<i") == 7
    assert html.count('class="done"') == 3
    assert html.count('class="now"') == 1


@_needs_node
def test_the_preview_frame_is_the_last_one_the_run_actually_wrote():
    """Requirement 8's thumbnail. The name is derived from the cadence and the count of finished
    forwards, so the page asks for a frame that exists instead of one it hopes for.
    """
    at_seven, at_two, from_stem = _node_eval("""
      const job = {args: ["generate", "--preview-every", "5"],
                   output_stem: "/o/ночь/h3-кот-896x576"};
      console.log(JSON.stringify([app.previewUrl(job, 7), app.previewUrl(job, 2),
                                  app.previewUrl({args: ["generate"],
                                                  output_stem: "h3-кот"}, 9)]));
    """)
    assert at_seven.endswith("-preview-step05.jpg"), at_seven
    assert at_seven.startswith("/media/%D0%BD%D0%BE%D1%87%D1%8C/"), at_seven
    assert at_two is None, "before the first frame there is no frame to show"
    assert from_stem is None, "`/media/<run>/<file>` needs a run segment; a bare stem has none"


@_needs_node
def test_the_running_job_is_matched_to_its_own_run_on_disk():
    """Requirement 8's numbers come from `runs.scan`, and the only thing tying a scanned run to a
    queued job is the output directory. Matching the wrong one would show a stranger's progress.
    """
    mine, none = _node_eval("""
      const runs = [{outdir: "/o/другая", completed: 9}, {outdir: "/o/ночь", completed: 3}];
      const job = {args: ["generate", "--outdir", "/o/ночь"]};
      console.log(JSON.stringify([app.runForJob(job, runs),
                                  app.runForJob({args: ["generate"]}, runs)]));
    """)
    assert mine["completed"] == 3, mine
    assert none is None, "a job that names no outdir matches no run"


@_needs_node
def test_the_output_directory_defaults_to_the_one_the_last_jobs_used():
    """Not one of the eleven, but the field is required and typing it in every evening is how a
    person ends up with three spellings of the same directory.
    """
    reused, fresh = _node_eval("""
      const state = {queue: {pending: [
        {created_at: "2026-08-12T21:00:00", args: ["generate", "--outdir", "/o/ночь"]},
        {created_at: "2026-08-12T19:00:00", args: ["generate", "--outdir", "/o/утро"]},
      ]}, runs: []};
      console.log(JSON.stringify([
        app.defaultOutdir(state, new Date("2026-08-12T22:00:00")),
        app.defaultOutdir({queue: {}, runs: []}, new Date("2026-08-12T22:00:00")),
      ]));
    """)
    assert reused == "/o/ночь", "the newest job's directory, not the first one scanned"
    assert fresh == "~/video-out/2026-08-12", fresh


@_needs_node
def test_finished_shows_the_exit_code_and_a_failure_shows_why():
    """Requirement 9. The reason is the worker's own `log_tail`; inventing a friendlier one would
    hide the only line that says what happened.
    """
    ok, failed = _node_eval("""
      const base = {id: "j", note: "", args: ["generate", "--tag", "кот"],
                    estimate: {width: 896, height: 576, duration_seconds: 10, steps: 8},
                    output_stem: "/o/ночь/h3-кот-896x576",
                    started_at: "2026-08-12T01:00:00", finished_at: "2026-08-12T02:00:00"};
      console.log(JSON.stringify([
        app.finishedRowHtml({...base, exit_code: 0}),
        app.finishedRowHtml({...base, exit_code: 1,
                             log_tail: "RuntimeError: [metal::malloc] 51.4 GB > 48.0 GB"}),
      ]));
    """)
    assert "код 0" in ok
    assert "код 1" in failed
    assert "metal::malloc" in failed, "a failed run with no visible reason is a mystery, not a row"
    assert 'href="/media/%D0%BD%D0%BE%D1%87%D1%8C/h3-%D0%BA%D0%BE%D1%82-896x576.mp4"' in ok, ok


@_needs_node
def test_only_the_last_day_of_finished_runs_is_shown():
    """Requirement 9's window. A queue that never forgets grows without bound and buries today."""
    kept = _node_eval("""
      const at = new Date("2026-08-12T22:00:00");
      const rows = app.finishedWithin([
        {id: "fresh", finished_at: "2026-08-12T20:00:00"},
        {id: "stale", finished_at: "2026-08-10T20:00:00"},
        {id: "yesterday", finished_at: "2026-08-12T01:00:00"},
      ], at);
      console.log(JSON.stringify(rows.map(r => r.id)));
    """)
    assert kept == ["fresh", "yesterday"], kept


@_needs_node
def test_unreadable_queue_files_get_a_line_of_their_own():
    """Requirement 10. They are in no list above, and a human counts the night's queue by the
    list -- silence here is a queue that is quietly one job short.
    """
    html, empty = _node_eval("""
      console.log(JSON.stringify([
        app.brokenHtml([{path: "queue/pending/20260811-2139-x.json", error: "JSONDecodeError"}]),
        app.brokenHtml([]),
      ]));
    """)
    assert "20260811-2139-x.json" in html and "не прочитался" in html
    assert empty == "", "no broken files, no line"


@_needs_node
def test_everything_a_human_typed_is_escaped_before_it_reaches_the_markup():
    """The tag, the note and the worker's `log_tail` are all strings this page did not write."""
    row = _node_eval("""
      console.log(JSON.stringify(app.pendingRowHtml({
        id: "j", priority: 0, note: "<img src=x onerror=alert(1)>",
        args: ["generate", "--tag", "<script>"], estimate: {seconds: 1, peak_gb: 1},
        output_stem: "/o/n/h3-x"})));
    """)
    assert "<script>" not in row and "&lt;script&gt;" in row
    assert "<img" not in row and "&lt;img" in row, "the note has to arrive as text, not as a tag"


@_needs_node
def test_the_argument_list_the_form_builds_is_one_the_server_accepts(queue_server):
    """The seam between the two halves of this task, tested across it.

    `buildArgs` runs in `node`, and its output is posted to the running server. Every other test
    here checks one side or the other; a rename of `--turbo-lora` to `--lora` would pass all of
    them and break the only thing the page exists to do.
    """
    lora = queue_server.models / "turbo.safetensors"
    lora.write_bytes(b"\x00")
    args = _node_eval("""
      console.log(JSON.stringify(app.buildArgs({
        prompt: "кот на подоконнике", width: 896, height: 576, duration: 10, steps: 31,
        seed: 3, tag: "ночь", mode: "t2va", checkpoint: "CKPT", outdir: "OUT",
        lora: "LORA", loraStrength: 0.45, image: "", endImage: "", promptFile: null,
      })));
    """)
    args = [str(queue_server.models / "ckpt") if a == "CKPT"
            else str(queue_server.outdir) if a == "OUT"
            else str(lora) if a == "LORA" else a for a in args]

    status, answer = _call(queue_server, "POST", "/api/estimate", {"args": args})
    assert status == 200, answer
    assert answer["estimate"]["width"] == 896 and answer["estimate"]["steps"] == 31, answer

    status, answer = _call(queue_server, "POST", "/api/jobs", {"args": args, "note": "с формы"})
    assert status == 200, answer
    assert "--turbo-lora" in answer["job"]["args"], "the LoRA flag has to be the one the CLI has"

    status, state = _json(queue_server, "/api/state")
    assert [job["id"] for job in state["queue"]["pending"]] == [answer["job"]["id"]], state


@_needs_node
def test_build_args_carries_the_adaln_cache_flag_only_when_the_field_is_set():
    """The project's working recipe needs `--adaln-cache` to bake 8 steps instead of 31 (see
    `h3_48gb/cli.py`'s `schedule_not_baked`), and until now the form had no field for it at all --
    a dry-run of the 8-step recipe was refused and the page silently fell back to 31 steps.

    An empty `#adaln` must not turn into `--adaln-cache ""`: the CLI reads a present flag as a
    real path and would refuse with `adaln_cache_unreadable` rather than just running without one.
    """
    base = {
        "prompt": "кот", "width": 896, "height": 576, "duration": 10, "steps": 8,
        "seed": 3, "tag": "ночь", "mode": "t2va", "checkpoint": "CKPT", "outdir": "OUT",
        "lora": "LORA", "loraStrength": 1, "image": "", "endImage": "", "promptFile": None,
    }
    with_table, without = _node_eval(f"""
      const base = {json.dumps(base)};
      console.log(JSON.stringify([
        app.buildArgs({{...base, adaln: "ADALN"}}),
        app.buildArgs({{...base, adaln: ""}}),
      ]));
    """)
    assert "--adaln-cache" in with_table, with_table
    assert with_table[with_table.index("--adaln-cache") + 1] == "ADALN", with_table
    assert "--adaln-cache" not in without, without


def test_the_form_defaults_to_the_project_s_working_recipe():
    """The project's working recipe is 8 steps, this checkpoint, this turbo LoRA at full strength,
    and this baked AdaLN table -- so a fresh page has to open ready to run it, not to a checkpoint
    that forces 31 steps because no one typed the table's path in yet.
    """
    page = _page_text("index.html")
    assert re.search(r'id="ckpt"[^>]*value="~/models/h3-8bit-full"', page), (
        "the checkpoint must default to the 8-bit build the recipe was measured on")
    assert re.search(
        r'id="lora"[^>]*value="~/models/turbo/minimax_h3_turbo_v4_step600_ema\.safetensors"', page)
    assert re.search(r'id="lora-str"[^>]*value="1\.00"', page)
    assert re.search(r'id="adaln"[^>]*value="~/models/turbo/adaln_8_l100\.safetensors"', page), (
        "without this default, --steps 8 is refused as schedule_not_baked and the page falls "
        "back to 31 steps")
    assert re.search(r'id="steps"[^>]*value="8"', page)


def test_editing_a_queued_job_restores_its_adaln_cache_flag():
    """Requirement 3: editing a pending job has to read `--adaln-cache` back out of its stored
    args, the same way it already does for `--turbo-lora` a line above it.
    """
    body = _js_function(_page_text("app.js"), "function fillFormFrom(job)")
    assert '$("adaln").value = argValue(job.args, "--adaln-cache") || "";' in body, body


def test_a_job_posted_through_the_api_is_in_the_next_state_the_page_polls(queue_server):
    """The whole loop the page runs on: post, poll, see it. Without this the page could be
    posting into a queue `/api/state` never reads and nothing would say so.
    """
    status, before = _json(queue_server, "/api/state")
    assert status == 200 and before["queue"]["pending"] == []

    status, answer = _call(queue_server, "POST", "/api/jobs",
                           {"args": _job_args(queue_server, tag="увидеть"), "note": "круг"})
    assert status == 200, answer

    status, after = _json(queue_server, "/api/state")
    posted = [job for job in after["queue"]["pending"] if job["id"] == answer["job"]["id"]]
    assert len(posted) == 1, after
    assert posted[0]["note"] == "круг"
    assert posted[0]["estimate"]["seconds"] > 0, "the page draws the queue's totals from this"


# == Круг 1 ревью =================================================================================


@pytest.mark.parametrize("args", [
    ["generate", "кот", "--tag", "a\x00b"],
    ["generate", "ко\x00т"],
    ["generate", "кот", "--seed", "1\x000"],
])
def test_a_nul_byte_in_an_argument_is_a_refusal_not_a_crash(queue_server, args):
    """`subprocess.run` raises `ValueError: embedded null byte` while preparing `execve`, and left
    alone that reaches the `internal_error` net -- a 500 for input the caller controls, the exact
    failure `resolve_within`'s docstring forbids and the one task 5 closed for `/static` URLs. The
    body was never covered: those tests all sent the NUL in a URL.
    """
    full = args + ["--outdir", str(queue_server.outdir),
                   "--checkpoint", str(queue_server.models / "ckpt")]
    status, answer = _call(queue_server, "POST", "/api/jobs", {"args": full, "note": ""})
    assert status == 400, answer
    assert answer["error"]["code"] == "args_invalid", answer
    assert _pending(queue_server) == []


def test_validate_args_names_which_argument_carried_the_nul():
    """Refused by name rather than by catching the exception it causes: an argument with a NUL in
    it is not an argument, and saying which one is worth more than reporting `ValueError`.
    """
    with pytest.raises(CliError) as excinfo:
        web.validate_args(["generate", "ок", "--tag", "a\x00b"])
    assert excinfo.value.code == "args_invalid"
    assert excinfo.value.detail["position"] == 3


def test_a_tag_too_long_for_a_filename_does_not_blame_the_queue_directory(queue_server):
    """The job id is built from the tag and never truncated, so a 240-character tag makes
    `queue/pending/<id>.json` longer than the 255 bytes the filesystem allows. `ENAMETOOLONG` used
    to travel up to `queue_errors` and answer `queue_unwritable`, 500 -- telling a person their
    queue directory is broken when it is perfectly healthy, and sending them to check permissions
    that have nothing to do with it.
    """
    status, answer = _call(queue_server, "POST", "/api/jobs",
                           {"args": _job_args(queue_server, tag="q" * 240), "note": ""})
    assert status == 400, answer
    assert answer["error"]["code"] == "path_outside_root", answer
    assert answer["error"]["code"] != "queue_unwritable"
    assert _pending(queue_server) == []
    assert (queue_server.queue_root / "pending").is_dir(), "the queue itself is fine"


def test_a_queue_that_really_cannot_be_written_is_still_a_500(queue_server, monkeypatch):
    """The other side of the test above, and the reason its guard is narrowed to one `errno`
    rather than "any `OSError` from the queue is the caller's fault". A permission failure really
    is the queue directory being unusable, and it has to keep its 500 and its path -- otherwise
    the fix for the false diagnosis would have replaced it with the opposite false diagnosis.
    """
    def refuse(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(q, "submit", refuse)
    status, answer = _call(queue_server, "POST", "/api/jobs",
                           {"args": _job_args(queue_server), "note": ""})
    assert status == 500, answer
    assert answer["error"]["code"] == "queue_unwritable", answer
    assert str(queue_server.queue_root) in answer["error"]["detail"]["path"]


@pytest.mark.parametrize("method,suffix", [("DELETE", ""), ("POST", "/top")])
def test_a_job_id_too_long_for_a_filename_is_a_400_without_queueing_anything(queue_server,
                                                                             method, suffix):
    """Reachable with a bare URL and no submission at all: `_job_id_of` resolves a 400-character
    id inside `pending/` quite happily, and `queue.cancel` is where the filesystem says no.
    `pathlib` swallows `ENOENT` and its friends but not this one, so it was a 500.
    """
    status, answer = _call(queue_server, method, f"/api/jobs/{'z' * 400}{suffix}")
    assert status == 400, answer
    assert answer["error"]["code"] == "path_outside_root", answer


@pytest.mark.parametrize("method,url", [("POST", "/api/jobs"), ("PUT", "/api/jobs/x"),
                                        ("DELETE", "/api/jobs/x")])
def test_two_origin_headers_are_refused_rather_than_read_by_the_first(queue_server, method, url):
    """`Message.get` returns the first occurrence, so `Origin: <this page>` followed by
    `Origin: http://evil.example` passed the comparison with the attacker's line sitting in the
    same request. Which of the two a proxy or the next reader believes is not this server's call.
    """
    raw = (f"{method} {url} HTTP/1.1\r\n"
           f"Host: {web.LOOPBACK}:{queue_server.port}\r\n"
           f"Origin: http://{web.LOOPBACK}:{queue_server.port}\r\n"
           f"Origin: http://evil.example\r\n"
           f"Content-Type: application/json\r\nContent-Length: 2\r\n"
           f"Connection: close\r\n\r\n{{}}").encode()
    status, answer = _raw_exchange(queue_server, raw)
    assert status == 403, answer
    assert answer["error"]["code"] == "origin_not_allowed", answer
    assert answer["error"]["detail"]["count"] == 2


def test_two_host_headers_are_refused_too(queue_server):
    """The same hole one header over. A single `Host` still has to be this server's address, so
    the pair "ours, then theirs" would otherwise pass the rebinding check as well.
    """
    raw = (f"GET /api/state HTTP/1.1\r\n"
           f"Host: {web.LOOPBACK}:{queue_server.port}\r\n"
           f"Host: evil.example\r\n"
           f"Connection: close\r\n\r\n").encode()
    status, answer = _raw_exchange(queue_server, raw)
    assert status == 403, answer
    assert answer["error"]["code"] == "host_not_allowed", answer


def test_one_origin_header_still_works(queue_server):
    """Paired with the two tests above: refusing every request that names an Origin would satisfy
    both, and would break the page.
    """
    raw = (f"POST /api/estimate HTTP/1.1\r\n"
           f"Host: {web.LOOPBACK}:{queue_server.port}\r\n"
           f"Origin: http://{web.LOOPBACK}:{queue_server.port}\r\n"
           f"Content-Type: application/json\r\nContent-Length: {len(_ESTIMATE_BODY)}\r\n"
           f"Connection: close\r\n\r\n").encode() + _ESTIMATE_BODY
    status, answer = _raw_exchange(queue_server, raw)
    assert status == 200, answer
    assert answer["estimate"]["forwards"] == 30


def test_the_two_provenance_refusals_have_different_codes(queue_server):
    """A wrong `Host` is an address a person typed and can retype; a wrong `Origin` is not their
    doing at all. One code for both would leave the page branching on `detail` keys to tell them
    apart, which is the thing codes exist to prevent.
    """
    from h3_48gb.cli import ERROR_CODES

    assert web.ERROR_STATUS["origin_not_allowed"] == 403
    assert {"host_not_allowed", "origin_not_allowed"} <= set(ERROR_CODES)
    assert ERROR_CODES["host_not_allowed"] != ERROR_CODES["origin_not_allowed"]

    by_host = _call(queue_server, "POST", "/api/estimate", {"args": ["generate", "x"]},
                    headers={"Host": "evil.example"})
    by_origin = _call(queue_server, "POST", "/api/estimate", {"args": ["generate", "x"]},
                      headers={"Origin": "http://evil.example"})
    assert by_host[1]["error"]["code"] == "host_not_allowed", by_host
    assert by_origin[1]["error"]["code"] == "origin_not_allowed", by_origin


#: What each page action may leave behind once it has raced `queue.claim`, as
#: `(state, seed, priority)` or `("gone",)`. Two outcomes each, one per order the lock granted.
_RACE_OUTCOMES = {
    "edit":   {(200, ("running", "2", 0)), (409, ("running", "1", 0))},
    "top":    {(200, ("running", "1", 1)), (409, ("running", "1", 0))},
    "cancel": {(200, ("gone",)),           (409, ("running", "1", 0))},
}


def _one_job_on_disk(live: _Live):
    jobs, broken = q.scan(live.queue_root)
    assert broken == [], broken
    assert len(jobs) <= 1, f"the job exists {len(jobs)} times: {[(j.id, j.state) for j in jobs]}"
    if not jobs:
        return ("gone",)
    job = jobs[0]
    return (job.state, job.args[job.args.index("--seed") + 1], job.priority)


@pytest.mark.parametrize("operation", ["edit", "top", "cancel"])
def test_a_page_action_racing_the_worker_leaves_exactly_one_job(queue_server, operation):
    """Races checked by racing, as the design spec requires -- two real threads on one job, not
    "claim, then edit".

    The queue's exclusive lock is what makes each action's check-and-write one step; from up here
    the visible proof is that whichever order the lock grants, the job exists exactly once
    afterwards and the HTTP answer matches the state on disk. A version that raced would show the
    job in `pending/` and `running/` at once, with the edit applied to the copy nobody runs.

    Both orders are named in one unconditional assertion rather than an `if`: a branch that never
    runs is a test that asserts nothing on the day it matters.
    """
    job = _queue_a_job(queue_server, "--seed", "1", tag="гонка")
    call = {
        "edit": lambda: _call(queue_server, "PUT", f"/api/jobs/{job['id']}",
                              {"args": _job_args(queue_server, "--seed", "2", tag="гонка"),
                               "note": ""}),
        "top": lambda: _call(queue_server, "POST", f"/api/jobs/{job['id']}/top"),
        "cancel": lambda: _call(queue_server, "DELETE", f"/api/jobs/{job['id']}"),
    }[operation]

    barrier = threading.Barrier(2)
    answered, claimed = [], []

    def act():
        barrier.wait(timeout=60)
        answered.append(call())

    def take():
        barrier.wait(timeout=60)
        claimed.append(q.claim(queue_server.queue_root))

    threads = [threading.Thread(target=act), threading.Thread(target=take)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=180)
        assert not thread.is_alive(), f"{operation} never finished"

    status, answer = answered[0]
    outcome = (status, _one_job_on_disk(queue_server))
    assert outcome in _RACE_OUTCOMES[operation], (outcome, answer, claimed)
    if status == 409:
        assert answer["error"]["code"] == "job_not_pending", answer


def test_a_route_must_say_which_body_fields_it_takes():
    """`allowed` is required and keyword-only, exactly like `_serve_file`'s `suffixes` after
    circle 3 of task 5. A default -- of anything at all -- makes forgetting the argument
    indistinguishable from a decision, and the decision here is which fields a route promises to
    honour. Forgetting it is a `TypeError` at the call site instead.
    """
    handler = web._Handler.__new__(web._Handler)
    with pytest.raises(TypeError):
        web._Handler._json_request(handler)


def test_two_sec_fetch_site_headers_are_refused_as_well(queue_server):
    """Found by a mutation that came back green: the duplicate-header rule was tested on `Origin`
    and on `Host` and not on the third header that uses it, so changing what that path answers
    changed nothing any test could see.
    """
    raw = (f"POST /api/estimate HTTP/1.1\r\n"
           f"Host: {web.LOOPBACK}:{queue_server.port}\r\n"
           f"Sec-Fetch-Site: same-origin\r\n"
           f"Sec-Fetch-Site: cross-site\r\n"
           f"Content-Type: application/json\r\nContent-Length: {len(_ESTIMATE_BODY)}\r\n"
           f"Connection: close\r\n\r\n").encode() + _ESTIMATE_BODY
    status, answer = _raw_exchange(queue_server, raw)
    assert status == 403, answer
    assert answer["error"]["code"] == "origin_not_allowed", answer
    assert answer["error"]["detail"]["header"] == "Sec-Fetch-Site"
    assert answer["error"]["detail"]["count"] == 2


@pytest.mark.parametrize("method,url,body,unknown", [
    ("PUT", "/api/prompts/new.txt", {"text": "сохрани", "prompt_source": "prompts/x.txt"},
     "prompt_source"),
    ("PUT", "/api/prompts/new.txt", {"text": "сохрани", "note": "заодно"}, "note"),
    ("POST", "/api/estimate", {"args": ["generate", "кот"], "note": "заодно"}, "note"),
    # The fourth route, and the one the first version of this test missed: jobs are *posted*
    # through `POST /api/jobs` and *edited* through `PUT /api/jobs/<id>`, and the rule had only
    # ever been checked on the first of the pair. `{job}` is filled in below with a job that
    # really is pending, and `{args}` with an argument list that really would be accepted --
    # without both, a widened allowlist would fail on the next check instead and the mutation
    # would come back green for the second time in this task.
    ("PUT", "/api/jobs/{job}", {"args": "{args}", "note": "", "prompt_text": "подложенный"},
     "prompt_text"),
])
def test_every_body_route_refuses_a_field_it_does_not_take(queue_server, method, url, body,
                                                            unknown):
    """Found twice by green mutations, on two routes, for the same reason both times: the only
    test touching the route sent a body that failed the **next** check anyway, so widening the
    allowlist changed nothing any assertion could see.

    Hence the setup below. A rule that holds on three routes out of four is not a rule, and a
    case whose request would be refused for a second reason does not test the first one.
    """
    if "{job}" in url:
        pending = _queue_a_job(queue_server, "--seed", "1", tag="правка")
        url = url.format(job=pending["id"])
        body = {**body, "args": _job_args(queue_server, "--seed", "2", tag="правка")}

    status, answer = _call(queue_server, method, url, body)
    assert status == 400, answer
    assert answer["error"]["code"] == "args_invalid", answer
    assert answer["error"]["detail"]["unknown"] == [unknown]
    assert not (queue_server.repo / "prompts" / "new.txt").exists(), (
        "the refusal happened after the write")
    assert all("2" not in job.args for job in _pending(queue_server)), (
        "the refusal happened after the edit was applied")


# -- Circle 1 of task 7: the page's own review ----------------------------------------------------


def _js_function(script: str, header: str) -> str:
    """The body of one function in `app.js`, cut out by matching braces from its declaration.

    A few claims about the page are about the DOM half, which `node` cannot call: what `submit`
    does *not* touch, which function `poll` calls, whether a scroll asks the system first. Those
    are checked on the source, the same way `test_the_page_asks_for_its_own_routes...` is. A check
    on the bytes is weaker than a call; it is also the difference between a claim that is checked
    and a claim that is asserted in a report and never looked at again.
    """
    opening = script.index("{", script.index(header) + len(header))
    depth = 0
    for i in range(opening, len(script)):
        depth += {"{": 1, "}": -1}.get(script[i], 0)
        if depth == 0:
            return script[opening + 1:i]
    raise AssertionError(f"{header} never closes")


def test_the_new_file_entry_carries_a_sentinel_that_survives_the_html_parser():
    """Found on the live page: choosing «— новый файл… —» turned red and wedged the select.

    The value was `"\\0new"`, and by the standard a parser must replace a NUL in an attribute
    value with U+FFFD -- in the browser the option's value really did arrive as `[65533, 110,
    101, 119]`, so both comparisons against `"\\0new"` were false forever. Python's `html.parser`
    does not perform that replacement, so the browser's behaviour cannot be reproduced here; what
    can be asserted is the thing that made it possible, which is a NUL in the markup at all.

    The second half matters as much: one literal, named once, compared against everywhere. Three
    copies of a sentinel is how one of them gets edited alone.
    """
    script = _page_text("app.js")
    assert "\0" not in script, (
        "a NUL in an attribute value is replaced by U+FFFD before any comparison sees it "
        "-- and it makes app.js a binary file for grep besides")

    named = re.search(r'const NEW_PROMPT = "([^"]+)";', script)
    assert named, "the sentinel has to be one named constant, not a literal repeated three times"
    sentinel = named.group(1)
    assert sentinel.isprintable() and sentinel == "__new__", sentinel
    assert not web.PROMPT_NAME.fullmatch(sentinel), (
        f"{sentinel!r} would collide with a real prompt file name")
    assert '<option value="${NEW_PROMPT}">' in script
    assert script.count("NEW_PROMPT") == 5, (
        "the constant, the option, and the three comparisons -- one in `savePrompt` (which asks "
        "for a name instead of saving over the placeholder), one in the select's own `change` "
        "handler, and one that refuses to open a chat about «— новый файл… —» -- three separate "
        "places, no other spelling")


def test_a_posted_job_leaves_every_field_but_the_seed_and_the_tag_alone():
    """Requirement 2's other half. `advanceAfterSubmit` is tested above and says what the seed and
    the tag become; nothing said what happens to the other thirteen fields, and the report claimed
    the requirement was checked automatically on the strength of that one test.

    The main use of this page is five jobs in an evening with one field changed each time. A
    `$("prompt").value = ""` added to `submit` would break exactly that, and until now nothing
    would have noticed.
    """
    body = _js_function(_page_text("app.js"), "async function submit()")
    written = set(re.findall(r"""\$\("([^"]+)"\)\.value\s*=[^=]""", body))
    assert written == {"seed", "tag"}, written


def test_the_prompt_list_is_reread_on_every_poll_and_not_only_at_startup():
    """A file written into `prompts/` while the page is open was invisible until a reload: the
    list was fetched once, in the startup block. The page already polls every twenty seconds, and
    the list belongs on that clock.
    """
    script = _page_text("app.js")
    assert "loadPromptList(" in _js_function(script, "async function poll()")
    startup = script[script.index("// -- запуск"):]
    assert "loadPromptList(" not in startup, "once at startup is the bug this replaced"
    assert 'loadPromptList($("prompt-file").value)' in script, (
        "rereading the list must keep the current choice, or every poll resets the select")


def test_the_page_stops_scrolling_smoothly_when_the_system_asks_it_to():
    """`@media (prefers-reduced-motion: reduce)` in the stylesheet covers everything the
    stylesheet animates. It cannot reach `window.scrollTo({behavior: "smooth"})`, which is asked
    for in script -- so the one animation the page starts by hand ignored the setting entirely.
    """
    body = _js_function(_page_text("app.js"), "function setEditing(id)")
    assert "prefers-reduced-motion" in body and "matchMedia" in body
    assert 'behavior: "smooth"' not in body, "the behaviour has to depend on the query"
    assert '"auto"' in body and '"smooth"' in body


@pytest.mark.parametrize("text,external", [
    ('<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter">', True),
    ('<script src="//cdn.jsdelivr.net/npm/chart.js"></script>', True),
    ("await fetch('http://192.168.1.5/api/state')", True),
    ("//TODO: the sentinel below", False),
    ("// the note explains why", False),
    ("const half = width//2", False),
])
def test_the_external_address_detector_knows_a_host_from_a_comment(text, external):
    """The detector above is the whole of `test_no_page_file_names_an_address_off_this_machine`,
    and it used to match any `//` followed by a letter -- `//TODO` included. A false positive
    there is not a harmless nit: the next person to hit it deletes the assertion, and the check
    that keeps a CDN out of this page goes with it.
    """
    assert bool(_EXTERNAL.search(text)) is external, text


@_needs_node
def test_an_hour_short_of_an_hour_is_not_printed_as_sixty_minutes():
    """The minutes were rounded inside the hour they belong to, so 3599 seconds came out as
    «60 мин» and 7199 as «1 ч 60 мин». Both are numbers no clock shows.
    """
    got = _node_eval("""
      console.log(JSON.stringify([3599, 7199, 3660, 59, 30, 29, 0].map(app.formatDuration)));
    """)
    assert got == ["1 ч", "2 ч", "1 ч 01 мин", "1 мин", "1 мин", "29 с", "0 с"], got


@_needs_node
def test_every_refusal_the_server_answers_with_403_has_a_russian_sentence():
    """Task 6 split the provenance refusal in two, and the page learned only the first half.

    The list is read out of `ERROR_STATUS` rather than written here, so the failure mode -- a
    third 403 arriving with no Russian text and falling through to the English one the server
    sent -- fails this test on the commit that adds it, not in a browser at three in the morning.
    """
    forbidden = sorted(code for code, status in web.ERROR_STATUS.items() if status == 403)
    assert len(forbidden) >= 2, forbidden
    titles, fallback = _node_eval("""
      const say = (code) => app.errorText({error: {code, message: "cross-site request"}}).title;
      console.log(JSON.stringify([%s.map(say), say("something_new")]));
    """ % json.dumps(forbidden))
    assert titles == ["Запрос пришёл не с этой страницы"] * len(forbidden), (forbidden, titles)
    assert fallback not in titles, "the sentence has to be chosen, not inherited from `default:`"


def test_the_unload_banner_is_laid_out_by_the_stylesheet_and_locks_while_it_works():
    """Плашка «выгрузить и начать» — единственный `.mem-warn` с кнопками внутри.

    Без собственных правил её два `<span>` вставали по-инлайновому: кнопки прилипали к тексту и
    переносились по одной. И без блокировки на время запроса она принимает второй клик — выгрузка
    это `pkill` плюс ожидание смерти порта, до десяти секунд молчания, а в молчание человек жмёт
    ещё раз.

    Обе половины проверяются текстом файла — браузера здесь нет; отпускание кнопки требуется
    именно в `finally`, потому что отказ сети — ровно тот случай, когда мёртвая кнопка стоит
    перезагрузки страницы.
    """
    css = _page_text("style.css")
    rule = re.search(r"\.unload-banner\s*\{([^}]*)\}", css)
    assert rule, "у #unload-banner нет собственного правила в style.css"
    assert "display: flex" in rule.group(1), rule.group(1)
    assert re.search(r"\.unload-banner-acts\s*\{[^}]*display:\s*flex", css), (
        "кнопки плашки должны стоять в ряд собственным правилом, а не по воле инлайн-потока")

    script = _page_text("app.js")
    body = _js_function(script, "async function unloadAndStart()")
    assert re.search(r'\$\("unload-banner-go"\)', body), body
    assert "disabled = true" in body and "finally" in body, body
    assert body.index("disabled = true") < body.index("api(\"POST\""), (
        "кнопка обязана глохнуть до запроса, а не после")


@_needs_node
def test_opening_a_chat_reads_the_session_once_even_though_the_hash_fires_late():
    """Открытие модалки — `location.hash = "#chat/<id>"`, и браузер ставит `hashchange` в очередь,
    а не зовёт обработчик тут же; страница поэтому синхронизируется руками сразу после
    присваивания. Сторож при этом смотрел на `chat`, который появляется только *после* `await` за
    сессией: в окне ожидания он пуст, `hashchange` приходит ровно в него, и та же сессия читается
    вторым GET и рисуется второй раз.

    Проверяется чистая функция-сторож на том самом порядке событий: «уже открывается» обязано
    закрывать дверь так же, как «уже открыто».
    """
    got = _node_eval("""
      const calls = [];
      let wanted = null;                       // то, что страница хранит рядом с `chat`
      function sync(hash) {                    // ровно тело `syncChatFromHash`
        const action = app.chatHashAction(hash, wanted);
        if (action.act === "close") { wanted = null; return; }
        if (action.act === "nothing") return;
        wanted = action.id;
        calls.push(action.id);                 // здесь страница уходит в `await enterChat`
      }
      sync("#chat/ab12");                      // ручной вызов сразу после присваивания хеша
      sync("#chat/ab12");                      // тот же адрес, но уже событием hashchange
      const opened = calls.slice();
      sync("#chat/ab12");                      // ещё один hashchange (F5 по тому же адресу)
      const still = calls.slice();
      sync("");                                // закрыли — и снова открыли ту же сессию
      sync("#chat/ab12");
      const reopened = calls.slice();
      sync("#chat/cd34");                      // другой разговор всё так же открывается
      console.log(JSON.stringify([opened, still, reopened, calls,
                                  app.chatHashAction("#not-a-chat", null),
                                  app.chatHashAction("#not-a-chat", "ab12"),
                                  app.chatHashAction("#chat/ZZZZ", null)]));
    """)
    opened, still, reopened, all_calls, no_hash, leaving, bad_id = got
    assert opened == ["ab12"], "программная установка хеша и его событие — одно открытие"
    assert still == ["ab12"], "повторный hashchange на том же адресе ничего не читает"
    assert reopened == ["ab12", "ab12"], "закрытая сессия открывается заново, а не глохнет"
    assert all_calls == ["ab12", "ab12", "cd34"]
    assert no_hash == {"act": "nothing"}, "без открытой сессии нечего закрывать"
    assert leaving == {"act": "close"}
    assert bad_id == {"act": "nothing"}, "id не из `secrets.token_hex` — не адрес сессии"


#: Two or more Latin letters in a row -- the shape an untranslated English word has. Single
#: letters are left alone (`t2va`, `flf`, a stray `N`), and the one place a Latin *name* belongs
#: in a sentence (`llama-server`, the program the person has to go and look at) is stripped by the
#: caller rather than weakened here.
_LATIN_WORD = re.compile(r"[A-Za-z]{2,}")
_PROVIDER_SOURCE = (PROJECT_ROOT / "h3_48gb" / "provider.py").read_text(encoding="utf-8")

#: Codes the chat modal can put in front of a person, and where each is raised. A literal list
#: because there is no single table to read them out of -- half are `_error_bytes(...)` inside
#: `web`'s chat routes and half are `ProviderError`s from `provider` -- but the provider half is
#: *checked* against the source below rather than trusted, which is the half that grows.
_CHAT_CODES = ("chat_not_found", "chat_busy", "chat_corrupt", "bad_image", "gpu_busy",
               "provider_unavailable", "llama_did_not_start", "chat_unreachable",
               "bad_model_json", "bad_provider_reply")


@_needs_node
def test_every_refusal_the_chat_can_produce_has_a_russian_sentence():
    """The whole chat modal fell through to `default:` — «Отказ: chat_unreachable», an English
    code on a Russian page, and the *same* sentence for «модель не подняли», «провайдер молчит»
    and «файл сессии сломан», which are three different things to go and do.

    Every code is also required to be in `ERROR_CODES`, so a sentence for a code the server
    cannot produce (a typo, or a code that was renamed on one side only) fails here too: a
    branch that never runs reads as coverage and is worse than the fallback it replaced.
    """
    for code in _CHAT_CODES:
        assert code in ERROR_CODES, f"{code!r} is not a code this system produces"
    titles, fallback = _node_eval("""
      const say = (code) => app.errorText({error: {code, message: "почему"}}).title;
      console.log(JSON.stringify([%s.map(say), say("code_nobody_wrote_a_sentence_for")]));
    """ % json.dumps(list(_CHAT_CODES)))
    for code, title in zip(_CHAT_CODES, titles):
        assert title != fallback.replace("code_nobody_wrote_a_sentence_for", code), (
            f"{code} still falls through to `default:`")
        assert code not in title, f"{code}'s sentence shows the code itself: {title!r}"
        assert not _LATIN_WORD.search(title.replace("llama-server", "")), (
            f"{code}'s sentence is not in Russian: {title!r}")
    assert len(set(titles)) == len(titles), (
        "two chat refusals sharing one sentence is the bug this test exists to catch: "
        f"{sorted(t for t in titles if titles.count(t) > 1)}")


@_needs_node
def test_every_provider_failure_reaches_the_page_with_a_sentence_of_its_own():
    """The list above is written by hand; this is what keeps it honest.

    `provider.py` is where a new chat failure gets invented (`ProviderError("...", ...)`), and it
    is two files away from the dictionary that has to name it. Reading the raise sites out of the
    source means the next one fails this test on the commit that adds it, rather than showing an
    English code to whoever hits it first.
    """
    raised = set(re.findall(r'ProviderError\(\s*"([a-z_]+)"', _PROVIDER_SOURCE))
    assert raised, "the raise sites moved -- this test is reading nothing"
    missing = raised - set(_CHAT_CODES)
    assert not missing, f"{sorted(missing)} can reach the page but is not in `_CHAT_CODES`"


@_needs_node
def test_a_finished_job_whose_timestamp_cannot_be_read_still_leaves_the_window():
    """«Закончилось за сутки» that never forgets grows without bound and buries today.

    A job whose `finished_at` will not parse is not thrown away -- it did finish, the moment is
    just not written down -- but it is dated by the next stamp that does parse. Only a job with
    no readable date at all stays, and the queue does not make those: `created_at` is written at
    submission.
    """
    kept = _node_eval("""
      const at = new Date("2026-08-12T22:00:00");
      console.log(JSON.stringify(app.finishedWithin([
        {id: "fresh", finished_at: "2026-08-12T20:00:00"},
        {id: "unreadable-but-recent", finished_at: "не время", created_at: "2026-08-12T19:00:00"},
        {id: "unreadable-and-old", finished_at: "", started_at: "2026-08-01T10:00:00"},
        {id: "no-date-at-all", finished_at: null},
      ], at).map((row) => row.id)));
    """)
    assert "fresh" in kept and "unreadable-but-recent" in kept, kept
    assert "unreadable-and-old" not in kept, (
        "a job that started eleven days ago did not finish in the last twenty-four hours")
    assert "no-date-at-all" in kept, "nothing to date it by is not a reason to hide it"


# -- Task 8: the chat modal -----------------------------------------------------------------------


@_needs_node
def test_a_model_turn_with_a_prompt_replaces_the_editor_text():
    """The whole point of the chat: a turn that carries a prompt rewrites the window, and the
    schema's fixed field order is what makes "collect them into text" a client-side job at all.
    """
    out = _node_eval("""
      const turn = {reply: "ок", prompt: {instruction: null,
        integrated_multimodal_description: "[Shot 1] X.",
        overall_soundscape: "Wind.", non_diegetic_music: "N/A"}};
      const state = {promptText: "старый", log: []};
      app.applyTurn(state, turn);
      console.log(JSON.stringify([state.promptText, state.log.length]));
    """)
    text, entries = out
    assert text.startswith("integrated_multimodal_description: [Shot 1] X.")
    assert "overall_soundscape: Wind." in text and text.endswith("non_diegetic_music: N/A\n")
    assert entries == 1


@_needs_node
def test_a_null_prompt_turn_leaves_the_editor_alone():
    """`prompt: null` is a reply without an edit -- a question back, a discussion. Wiping the
    window on one of those would throw away text nobody asked to change.
    """
    out = _node_eval("""
      const state = {promptText: "нетронутый", log: []};
      app.applyTurn(state, {reply: "а какой свет?", prompt: null});
      console.log(JSON.stringify(state.promptText));
    """)
    assert out == "нетронутый"


@_needs_node
def test_an_i2v_prompt_puts_the_instruction_first_with_a_blank_line():
    """The keyframe instruction is not one of the three fields and carries no header of its own:
    it opens the prompt and is separated from the fields by a blank line, exactly as the format
    document writes it.
    """
    out = _node_eval("""
      console.log(JSON.stringify(app.buildPromptText({
        instruction: "For the target video, at 0.00 seconds …",
        integrated_multimodal_description: "[Shot 1] X.",
        overall_soundscape: "Wind.", non_diegetic_music: "N/A"})));
    """)
    assert out.startswith("For the target video") and "\n\nintegrated_multimodal_description:" in out


@_needs_node
def test_a_turn_refused_because_the_gpu_is_busy_says_how_long_the_run_still_has():
    """`gpu_busy` on its own is a fact with no advice in it. The page already holds the running
    job's remaining seconds -- it prints them in the rail every twenty seconds -- so the plate
    says when the model *will* be able to answer, and a run with no estimate says nothing rather
    than «~0 мин».
    """
    with_estimate, without, unreachable = _node_eval("""
      const busy = {error: {code: "gpu_busy", message: "идёт прогон"}};
      console.log(JSON.stringify([
        app.chatFailureText(busy, 4200),
        app.chatFailureText(busy, 0),
        app.chatFailureText({error: {code: "chat_unreachable", message: "connection refused"}}, 0),
      ]));
    """)
    assert "прогон" in with_estimate and "1 ч 10 мин" in with_estimate, with_estimate
    assert "~" not in without, without
    assert "connection refused" in unreachable, "a 502 shows the provider's own words"


@_needs_node
def test_finishing_a_chat_about_a_job_puts_the_new_text_into_its_arguments():
    """«обновить задачу» is a `PUT /api/jobs/<id>` with the job's own arguments, and the queue
    takes the prompt from those arguments alone. A job queued from a file carries
    `--prompt-file <снимок>` pointing at `queue/prompts/<id>.txt`; leaving that flag in place
    would answer 200 and run the old text, because the snapshot is what the worker reads.
    """
    positional, from_file, glued, read_back = _node_eval("""
      console.log(JSON.stringify([
        app.argsWithPrompt(["generate", "старый текст", "--tag", "кот"], "новый"),
        app.argsWithPrompt(["generate", "--prompt-file", "/q/prompts/j1.txt", "--tag", "кот"],
                           "новый"),
        app.argsWithPrompt(["generate", "--prompt-file=/q/prompts/j1.txt", "--tag", "кот"],
                           "новый"),
        [app.promptOfArgs(["generate", "старый текст", "--tag", "кот"]),
         app.promptOfArgs(["generate", "--prompt-file", "/q/p.txt"])],
      ]));
    """)
    assert positional == ["generate", "новый", "--tag", "кот"]
    assert from_file == ["generate", "новый", "--tag", "кот"], (
        "--prompt-file must not survive: the worker reads the snapshot, not the new text")
    assert glued == ["generate", "новый", "--tag", "кот"], "--prompt-file=x is the same flag"
    assert read_back == ["старый текст", None]


@_needs_node
def test_an_answer_that_arrives_after_the_modal_moved_on_lands_nowhere():
    """Fix round 1, C1 and C2. A turn takes tens of seconds, and the modal is closeable (Esc, the
    backdrop, «закрыть») and re-openable on another session the whole time.

    Both halves were live bugs. Landing on a closed modal threw `TypeError` inside the `await`'s
    own `try`, so the `catch` threw again on `chat.log` and the `finally` never re-enabled
    «отправить» -- the button stayed dead until a reload. Landing on a *different* session wrote
    someone else's reply into it and overwrote its prompt window.

    Both are one rule -- the answer belongs to the state object that asked for it -- so both
    landings are checked here, on the same three cases.
    """
    dropped_closed, dropped_other, other_text, other_entries, landed, text = _node_eval("""
      const turn = {reply: "ок", prompt: {instruction: null,
        integrated_multimodal_description: "[Shot 1] X.",
        overall_soundscape: "Wind.", non_diegetic_music: "N/A"}};
      const expected = {id: "aaa", promptText: "свой", savedText: "свой", log: []};
      const other = {id: "bbb", promptText: "чужой", savedText: "чужой", log: []};
      const closed = app.landTurn(null, expected, turn);
      const foreign = app.landTurn(other, expected, turn);
      const ours = app.landTurn(expected, expected, turn);
      console.log(JSON.stringify([closed, foreign, other.promptText, other.log.length,
                                  ours, expected.promptText]));
    """)
    assert dropped_closed is False, "a closed modal takes no answer -- and must not throw"
    assert dropped_other is False and other_text == "чужой" and other_entries == 0, (
        "the answer of one session must not land in another")
    assert landed is True and text.startswith("integrated_multimodal_description")

    closed, foreign, other_entries, landed, log = _node_eval("""
      const payload = {error: {code: "chat_unreachable", message: "connection refused"}};
      const expected = {id: "aaa", promptText: "свой", savedText: "свой",
                        log: [{role: "user", text: "мрачнее"}]};
      const other = {id: "bbb", promptText: "чужой", savedText: "чужой", log: []};
      console.log(JSON.stringify([
        app.landFailure(null, expected, payload, 0),
        app.landFailure(other, expected, payload, 0),
        other.log.length,
        app.landFailure(expected, expected, payload, 0),
        expected.log.map((entry) => entry.role + "/" + (entry.kind || "")),
      ]));
    """)
    assert closed is False and foreign is False and other_entries == 0
    assert landed is True
    assert log == ["note/bad"], (
        "the message goes back to the input box, so its optimistic line leaves the transcript")


@_needs_node
def test_the_plate_says_the_run_is_in_the_way_rather_than_the_model_being_down():
    """Fix round 1, I1 (the brief's own wording). `gpu_busy` used to leave the plate saying «модель
    не поднята — поднимется при первом сообщении» directly above a transcript entry saying a
    generation is in the way. Two answers to one question, and the wrong one is the bigger one.
    """
    status, busy, busy_blind, down, external = _node_eval("""
      const payload = {error: {code: "gpu_busy", message: "идёт прогон"}};
      const state = {id: "a", promptText: "п", savedText: "п", log: []};
      app.landFailure(state, state, payload, 4200);
      console.log(JSON.stringify([state.llmStatus,
        app.llmPlateText("busy", {runningSeconds: 4200}),
        app.llmPlateText("busy", {runningSeconds: 0}),
        app.llmPlateText("down", {}),
        app.llmPlateText("down", {external: true})]));
    """)
    assert status == "busy", "a turn refused by the queue must not leave the plate saying `down`"
    assert "прогон" in busy and "1 ч 10 мин" in busy, busy
    assert "~" not in busy_blind, "a run with no estimate promises no minutes"
    assert "не поднята" in down and down != busy
    assert "внешний" in external, "there is nothing to raise for a provider on the internet"


@_needs_node
def test_hand_edits_in_the_window_are_noticed_before_the_modal_closes():
    """Fix round 1, I2. The session on disk holds the model's own answers (`prompt_struct`) and
    nothing else: an edit made by hand between two turns exists in the browser and nowhere else.
    Esc and a click on the backdrop are one keystroke away, so the page has to know it is about to
    throw work away.
    """
    fresh, edited, gone, after_turn = _node_eval("""
      const state = {id: "a", promptText: "текст", savedText: "текст", log: []};
      const before = app.hasUnsavedEdits(state);
      state.promptText = "текст, поправленный руками";
      const after = app.hasUnsavedEdits(state);
      const turn = {reply: "ок", prompt: {instruction: null,
        integrated_multimodal_description: "[Shot 1] X.",
        overall_soundscape: "Wind.", non_diegetic_music: "N/A"}};
      app.landTurn(state, state, turn);
      console.log(JSON.stringify([before, after, app.hasUnsavedEdits(null),
                                  app.hasUnsavedEdits(state)]));
    """)
    assert fresh is False and gone is False
    assert edited is True, "an edit nobody has seen but the browser is unsaved work"
    assert after_turn is False, (
        "the model's own answer is not an unsaved edit -- asking about it would train the person "
        "to dismiss the question")


@_needs_node
def test_an_empty_window_refuses_to_overwrite_the_prompt_or_the_job():
    """Fix round 1, I3. «сохранить промпт» on an empty window truncated a file in `prompts/` to
    zero bytes, and «обновить задачу» queued `args = ["generate", ""]`. One click each, no
    confirmation, nothing to undo it with.
    """
    empty, blank, real = _node_eval("""
      const of = (text) => app.finishRefusal({id: "a", promptText: text, savedText: "", log: []});
      console.log(JSON.stringify([of(""), of("   \\n  "), of("integrated_multimodal_description: X")]));
    """)
    assert empty and "пуст" in empty, empty
    assert blank == empty, "whitespace is not a prompt either"
    assert real is None, "a real prompt is saved without a word"


@_needs_node
def test_the_unload_banner_shows_only_when_jobs_wait_on_a_loaded_model():
    """Task 9. The worker will not take a job while llama's port is alive (Task 6), so a pending
    queue with the model up is not "about to start" -- it is stuck, and the banner is the only way
    off that short of the chat modal.
    """
    waiting_and_up, empty_and_up, waiting_and_down = _node_eval("""
      console.log(JSON.stringify([
        app.unloadBanner({pending: 2, llm: "up"}),
        app.unloadBanner({pending: 0, llm: "up"}),
        app.unloadBanner({pending: 2, llm: "down"}),
      ]));
    """)
    assert waiting_and_up["show"] is True
    assert waiting_and_up["text"] == "Модель в памяти держит GPU — выгрузить и начать генерацию?"
    assert empty_and_up["show"] is False
    assert waiting_and_down["show"] is False


@_needs_node
def test_dismissing_the_banner_holds_until_the_state_actually_changes():
    """«Пусть ждёт» must not be a snooze that reappears on the very next poll: the page reruns
    `unloadBanner` every twenty seconds against the same `{pending, llm}` while nothing else has
    happened, and popping the banner back up on an unchanged state trains the click to be ignored.
    A real change -- another job queued, the model unloaded some other way -- has to bring it back.
    """
    same_state_again, after_pending_changed, after_llm_changed = _node_eval("""
      const state = {pending: 2, llm: "up"};
      const dismissedKey = app.bannerKey(state);
      const rerender = app.unloadBannerVisible(state, dismissedKey);
      const grew = app.unloadBannerVisible({pending: 3, llm: "up"}, dismissedKey);
      const unloaded = app.unloadBannerVisible({pending: 2, llm: "down"}, dismissedKey);
      console.log(JSON.stringify([rerender, grew, unloaded]));
    """)
    assert same_state_again is False, "an unchanged state must not re-show a dismissed banner"
    assert after_pending_changed is True, "a new pending job must ask again"
    assert after_llm_changed is False, (
        "llm going down on its own makes unloadBanner().show false already -- nothing to show")


@_needs_node
def test_a_dismissal_burns_off_once_the_state_moves_past_it():
    """Fix round 1 (review, Important). `bannerKey` alone remembers only the *value* that was
    dismissed, never that the page ever left it: pending 2 -> «пусть ждёт» (dismissedKey "2:up")
    -> pending 3 (banner correctly reappears, unrelated to the fix) -> a job gets deleted and
    pending drops back to 2 -- the stale `dismissedKey` matches "2:up" again and the banner goes
    silent on a warning nobody has dismissed *this time*. The fix has to notice the state moved on
    and let the old dismissal expire, which only a function carrying the previous render's
    dismissal forward (not a pure `(state, dismissedKey) -> bool`) can do.
    """
    trace = _node_eval("""
      let banner = {dismissedKey: null};
      const step = (state) => { banner = app.nextBannerState(banner, state); return banner.show; };
      const seen = [];
      seen.push(step({pending: 2, llm: "up"}));                                    // first sight
      banner = {dismissedKey: app.bannerKey({pending: 2, llm: "up"})};             // «пусть ждёт»
      seen.push(step({pending: 2, llm: "up"}));                                    // held down
      seen.push(step({pending: 3, llm: "up"}));                                    // state moved
      seen.push(step({pending: 2, llm: "up"}));                                    // back again
      console.log(JSON.stringify(seen));
    """)
    assert trace == [True, False, True, True], (
        f"a return to the exact dismissed value after the state moved on must warn again: {trace}")
