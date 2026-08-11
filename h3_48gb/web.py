"""The localhost HTTP server: path policy, static and media reads, queue state.

Three things live here and nothing else does: the **path policy** that decides which absolute paths
a request may name, the **worker probe** that answers "is a worker running" without changing the
answer by asking, and the **routes** that read state off disk. Job submission (task 6) and the page
itself (task 7) build on this module; the layering is deliberate, because the path policy is the
only part of the server whose bugs are security bugs.

**No `mlx` import, ever** -- not at import time and not while serving a request. The server process
sits resident all day next to a 36 GB generation; a second copy of the MLX stack in it is memory
this machine does not have. Validation that genuinely needs MLX is done by a subprocess
(`generate --dry-run`, task 6), never in here. See `test_web_module_does_not_import_mlx` and
`test_serving_requests_never_imports_mlx`.

**Loopback only** (`LOOPBACK`). No authentication, no TLS, no bind address flag -- and localhost is
not an excuse to skip the path checks below: a browser runs other sites' JavaScript, and a request
to this server can arrive from a page nobody here wrote.

**The response is always JSON, including every failure.** `BaseHTTPRequestHandler` answers its own
refusals (an unsupported method, a malformed request line) with an HTML page; `_Handler.send_error`
overrides that, so there is no path through this module that emits HTML to an API client.

Comments and docstrings are English, matching the rest of the package; the Russian a human reads is
produced by the page rendering an error `code`, exactly as `queue.py`'s module docstring describes.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import mimetypes
import os
import re
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from h3_48gb import queue as q
from h3_48gb import runs as runs_module
from h3_48gb.cli import CliError
from h3_48gb.worker import WORKER_LOCK_NAME

#: The only address this server ever binds. Not a parameter, and deliberately not one: a flag that
#: could hold `0.0.0.0` is a flag someone eventually sets, and this server has no authentication of
#: any kind behind which that would be survivable.
LOOPBACK = "127.0.0.1"

#: Default port for `h3 web`. High, fixed, and unprivileged so the page can be bookmarked.
DEFAULT_PORT = 8765

#: The repository root -- `h3_48gb/web.py` -> `h3_48gb/` -> repo. `resolve()` because every
#: comparison in `resolve_within` is between resolved paths, and on macOS the repo can easily sit
#: under a symlinked directory.
REPO_ROOT = Path(__file__).resolve().parent.parent

#: Where the page's three files live. **Not** the repository root: `/static/../cli.py` never leaves
#: the repository, so a policy that only asked "is it inside the repo" would serve this project's
#: source to anything that asked. The static root has to be the leaf directory itself.
WEBUI_ROOT = Path(__file__).resolve().parent / "webui"

#: Roots whose contents may be read but never written. `models` holds 46 GB of weights that took
#: hours to convert and that nothing on this page has any business replacing.
READ_ONLY_ROOTS = frozenset({"models"})

#: Every flag of `h3 generate` whose value is a path, and what a run does with it. Kept here rather
#: than at each call site so that a new path flag cannot be added to the CLI without a policy
#: decision -- `test_check_path_flags_covers_every_flag_the_parser_knows_about` reads the flag list
#: back out of `build_parser()` and fails when this dict falls behind. Task 6 uses it on submission.
PATH_FLAGS = {
    "--prompt-file": "read", "--image": "read", "--end-image": "read",
    "--checkpoint": "read", "--adaln-cache": "read", "--turbo-lora": "read",
    "--outdir": "write", "--checkpoint-dir": "write", "--preview-stem": "write",
}

#: The only file types `/media` will serve. An **allowlist**, and that shape is the whole point.
#:
#: Circle 1 bounded `/media` to a run directory and excluded the queue by comparing its name.
#: Circle 2 broke that with one capital letter: `Path.resolve()` does not canonicalise case, this
#: machine's APFS volume is case-insensitive, and `/media/QUEUE/pending/job.json` therefore missed
#: a name-based exclusion and read the queue. A denylist by name cannot work on a case-insensitive
#: filesystem, and patching it per spelling only waits for the next one.
#:
#: One rule instead covers `queue/pending/*.json`, `queue/logs/*.log`, `queue/prompts/*.txt`,
#: `queue/results/*` and whatever directory someone puts beside them next -- however the name is
#: spelled. It also closes a hole nobody was looking for: `<outdir>/checkpoints` is the default
#: `--checkpoint-dir`, so `/media/checkpoints/h3-*.safetensors` was serving multi-gigabyte resume
#: weights through `read_bytes()`, whole, into this process's memory.
MEDIA_SUFFIXES = frozenset({".mp4", ".jpg", ".jpeg", ".png", ".wav"})


class _AnySuffix:
    """The allowlist that allows everything, for a route whose root is its own bound.

    A sentinel object rather than `None`, and `_serve_file`'s `suffixes` is required rather than
    defaulting to it, so that "serve any type" is something a route *says*. The previous shape --
    `suffixes=None` meaning "anything" -- made forgetting the argument identical to opening the
    route, and mutation C2b (drop the argument at the one call site that has it) is that bug
    written out. Only the single call site being under test kept it visible.
    """

    __slots__ = ()

    def __contains__(self, item) -> bool:
        return True

    def __repr__(self) -> str:
        return "ANY_SUFFIX"


#: `/static` passes this: its root is `webui/`, a directory nothing writes into at run time, so the
#: directory bound is the whole policy and a type allowlist would add nothing.
ANY_SUFFIX = _AnySuffix()

#: Prompt file names the page may name, per the design spec's "Пути". A bare name with a `.txt`
#: suffix and nothing that a filesystem reads as structure -- no separator, no `..`, no leading dot.
PROMPT_NAME = re.compile(r"[A-Za-z0-9_-]+\.txt\Z")

#: Where the prompts a run can be pointed at live, relative to the repository root. They are files
#: in git on purpose (see the design spec): a comparison only means something when the prompt is
#: byte-identical.
PROMPTS_DIR = "prompts"

#: HTTP status for each `CliError` code that is not a plain refusal of the request. Everything
#: absent from here is 400: the caller asked for something this server will not do.
#:
#: `job_not_pending` is **409**, not 400: that request was valid and lost a race with the worker
#: rather than being wrong -- the job left `pending/` between the page's last poll and this
#: request. It is mapped here *before* task 6 raises it, on purpose; see `PLANNED_CODES`.
ERROR_STATUS = {
    "host_not_allowed": 403,
    "job_not_pending": 409,
    "queue_unwritable": 500,
    "internal_error": 500,
}

#: Codes `ERROR_STATUS` maps ahead of the commit that raises them, so that the failure mode
#: "someone added the code and the raise, and forgot the status" cannot happen -- the status is
#: already there.
#:
#: This replaces a conditional test (`if "job_not_pending" in ERROR_CODES: assert ...`) which had
#: zero assertions until the day it mattered: coverage called it green, and the first tidy-up of
#: "empty tests" would have deleted the failure mode along with it. A named list of data is a
#: worse hiding place than a test that does nothing.
#:
#: It is meant to stay nearly empty. `test_no_planned_code_has_already_arrived` fails once a code
#: here lands in `ERROR_CODES`, which forces it out of this set and back under the ordinary check.
PLANNED_CODES = frozenset({"job_not_pending"})


def models_root() -> Path:
    """Where the weights live: `$H3_MODELS_ROOT`, or `~/models`.

    Read per call rather than frozen at import so a test (and a machine with the weights elsewhere)
    can set the variable without reloading the module, exactly as `cli.DEFAULT_OUTDIR`'s `H3_OUTDIR`
    is read -- except that one *is* frozen at import, which is why this is a function and not a
    module constant.
    """
    return Path(os.environ.get("H3_MODELS_ROOT") or Path.home() / "models")


def default_roots(outdir) -> dict[str, Path]:
    """The three roots a request's paths may point into.

    Three, not two. The repository holds the prompts and the code; `H3_OUTDIR` holds the output and
    the queue; and `~/models` holds the weights, read-only. The third one is not a convenience: the
    CLI's own default checkpoint is `~/models/h3-converted`, so a two-root policy would refuse
    `h3 generate`'s default value for `--checkpoint` -- see
    `test_the_clis_own_default_checkpoint_is_inside_a_root`.
    """
    return {"repo": REPO_ROOT, "outdir": Path(outdir), "models": models_root()}


def resolve_within(path, roots: dict[str, Path], *, write: bool) -> Path:
    """`path` as an absolute, symlink-free path inside one of `roots`, or `path_outside_root`.

    The comparison is between `Path.resolve()` results on both sides, never between strings. Text
    is not enough twice over: `<root>/../../etc/passwd` starts with the root as a *prefix*, and a
    symlink inside a root can point anywhere at all while its name stays perfectly innocent.
    `resolve()` is what sees through both, and it is non-strict, so a path that does not exist yet
    (an `--outdir` about to be created) resolves to what it would be rather than raising.

    `write=True` drops every root in `READ_ONLY_ROOTS` from consideration, which is the entire
    mechanism behind "the models root is readable but never writable": there is no separate check
    to forget, the root simply is not in the set being searched.
    """
    try:
        resolved = Path(path).expanduser().resolve()
    except (ValueError, OSError, RuntimeError) as exc:
        # A NUL byte (`/static/%00../cli.py`) makes `resolve()` raise `ValueError`, and `~nobody`
        # makes `expanduser()` raise `RuntimeError`. Both arrive from outside, so both are the
        # caller asking for something that is not a path -- a refusal, not a 500. Reaching the
        # handler's `internal_error` net instead would report an attacker-controlled input as a
        # bug in this server.
        raise CliError(
            "path_outside_root",
            f"not usable as a filesystem path: {path!r} ({type(exc).__name__}: {exc})",
            {"path": str(path), "write": write},
        ) from exc
    allowed = {name: root for name, root in roots.items()
               if not (write and name in READ_ONLY_ROOTS)}
    for root in allowed.values():
        root_resolved = Path(root).expanduser().resolve()
        if resolved == root_resolved or resolved.is_relative_to(root_resolved):
            return resolved
    raise CliError(
        "path_outside_root",
        f"path is outside every root this server may {'write to' if write else 'read'}: {path}",
        {"path": str(path), "resolved": str(resolved), "write": write,
         "roots": {name: str(root) for name, root in allowed.items()}},
    )


def check_path_flags(args: list[str], roots: dict[str, Path]) -> None:
    """Refuse an `h3` argument list that points any of its path flags outside `roots`.

    Both spellings argparse accepts are checked -- `--outdir /etc` and `--outdir=/etc` -- because
    the second one is a perfectly ordinary way to write it and a checker that only understood the
    first would be a hole with a test suite in front of it.

    A flag with no value after it is left alone: `argparse` in the validation subprocess is what
    reports that, with a better message than this function could invent.
    """
    index = 0
    while index < len(args):
        token = args[index]
        flag, equals, inline = token.partition("=")
        if flag not in PATH_FLAGS:
            index += 1
            continue
        if equals:
            value = inline
        elif index + 1 < len(args):
            value = args[index + 1]
            index += 1
        else:
            index += 1
            continue
        resolve_within(value, roots, write=PATH_FLAGS[flag] == "write")
        index += 1


def resolve_prompt_name(name: str, repo=REPO_ROOT) -> Path:
    """`prompts/<name>` inside the repository, or `prompt_name_invalid`.

    A prompt is addressed by name, not by path: the page offers the files in one directory and
    nothing else, so anything a filesystem would read as structure -- a separator, a `..`, an
    absolute path, a suffix other than `.txt` -- is refused before it can reach `resolve_within`.
    The `resolve_within` call still happens afterwards, as a second, independent line: the name
    rule is a whitelist and whitelists get widened, and when this one does the path check must
    still be the thing standing behind it.

    Used by task 6's and task 7's `/api/prompts` routes. It lives here, with the rest of the path
    policy, because the design spec puts the rule here ("Пути") and because a rule that ships in
    the same commit as the checks it belongs beside cannot be forgotten when the route is written.
    """
    if not PROMPT_NAME.fullmatch(name or ""):
        raise CliError(
            "prompt_name_invalid",
            f"a prompt name must match {PROMPT_NAME.pattern} and name no directory: {name!r}",
            {"name": name},
        )
    return resolve_within(Path(repo) / PROMPTS_DIR / name, {"repo": Path(repo)}, write=False)


@contextlib.contextmanager
def queue_errors(queue_root):
    """Turn any `OSError` raised while touching the queue into `queue_unwritable`, with the path.

    Scoped to the queue on purpose rather than wrapped around the whole handler: an `OSError` from
    reading a static file is not the queue being unwritable, and reporting it as such would send
    someone to check the wrong directory's permissions. Everything else still reaches the handler's
    `internal_error` net.
    """
    try:
        yield
    except OSError as exc:
        raise CliError(
            "queue_unwritable",
            f"the queue directory is not usable: {queue_root} ({exc})",
            {"path": str(queue_root), "error": f"{type(exc).__name__}: {exc}"},
        ) from exc


def worker_state(queue_root) -> str:
    """`"alive"`, `"stopped"` or `"unknown"` -- whether a worker holds `queue/worker.lock`.

    **This probe must not create the lock file.** `worker.hold_worker_lock` opens it with
    `O_CREAT`, which is right for a worker and wrong for a question: a server that probed the same
    way would conjure a `worker.lock` into existence on the first `/api/state` of a queue that has
    never had a worker, and then keep answering questions about a file it invented. A missing file
    is not an obstacle to answering -- `flock` cannot outlive the process that took it, so no file
    means no holder, which is the whole answer.

    `O_RDONLY` for the same reason it is not `O_RDWR`: `flock` does not care about the fd's access
    mode, and asking for less means the probe still works on a queue directory mounted read-only.

    **The probe takes the lock for microseconds, and that is visible from outside.** `LOCK_EX`
    then `LOCK_UN` is the only way `flock` answers the question at all, so a worker starting in
    that window would see `BlockingIOError` and die with `worker_already_running`, naming a worker
    that is really this probe. `worker.hold_worker_lock` retries once for exactly this reason --
    the two modules are coupled here and the comment exists in both.

    For the same reason this must not be called from a process that is itself holding the lock:
    `flock` is per-process, so the probe would re-acquire its own lock, answer `"stopped"`, and
    then **release it**. The server and the worker are separate processes, which is what makes
    that safe; `queue.lease_is_free` carries the same caveat for the same reason.

    `"unknown"` is reserved for a probe that genuinely could not run -- the path is a directory,
    the filesystem has no `flock`. It is never a guess in either direction: `/api/state` says
    "unknown" and the page says so too, rather than telling a human the queue is about to move
    when nothing is going to move it.
    """
    path = Path(queue_root) / WORKER_LOCK_NAME
    try:
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        return "stopped"
    except OSError:
        return "unknown"
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "alive"
        except OSError:
            return "unknown"
        fcntl.flock(fd, fcntl.LOCK_UN)
        return "stopped"
    finally:
        os.close(fd)


def build_state(queue_root, outdir) -> dict:
    """Everything the page polls for, in one document: the worker, the queue, the runs.

    One endpoint rather than three because the page redraws as a whole every 20 seconds, and three
    requests could show a job as `pending` in one and `running` in another -- a flicker with no
    cause a reader could ever diagnose.

    `pending` is returned in the order the worker will actually claim it (`(-priority, id)`, see
    `queue.claim`), not in filename order: the page's first job is the next job, and "move to
    front" has to be visible as a move. The other three states keep `scan`'s name order.

    Unparseable job files come back in `queue.broken` rather than being dropped -- a queue that is
    quietly one job short is the failure `scan` was written to prevent.
    """
    with queue_errors(queue_root):
        jobs, broken = q.scan(queue_root)
        state = worker_state(queue_root)

    grouped: dict[str, list[dict]] = {name: [] for name in q.QUEUE_STATES}
    for job in jobs:
        grouped[job.state].append(job.as_dict())
    grouped["pending"].sort(key=lambda row: (-int(row.get("priority") or 0), row["id"]))

    return {
        "ok": True,
        "worker": {"state": state},
        "queue": {**grouped,
                  "broken": [{"path": item.path, "error": item.error} for item in broken]},
        "runs": [run.as_dict() for run in runs_module.scan(Path(outdir))],
    }


def _json_bytes(payload) -> bytes:
    """`payload` as the bytes of one JSON document. `ensure_ascii=False` because half the tags and
    notes on this machine are Russian and escaping them makes the wire format unreadable in a log.
    """
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"


def _error_bytes(code: str, message: str, detail: dict | None = None) -> bytes:
    """The one failure shape, identical to `CliError.to_dict()` and to what the CLI prints."""
    return _json_bytes({"ok": False,
                        "error": {"code": code, "message": message, "detail": detail or {}}})


#: The code each router-level refusal answers with -- the ones this module produces before any
#: `CliError` exists, or that `BaseHTTPRequestHandler` produces on its own behalf.
#:
#: These live in `cli.ERROR_CODES` alongside the CLI's own, and that is a correction from review
#: circle 1. The first version kept them out on the argument that `ERROR_CODES` documents what
#: `CliError` raises; but the contract the design spec fixes is the one **on the wire** ("контракт
#: один на CLI, работника и сервер"), and these go over that wire on the two most ordinary
#: failures there are -- a typo in the address and the wrong method. Task 7's page turns a `code`
#: into a Russian sentence; with them missing it would fall into its catch-all on exactly those
#: two, and nothing would have stopped a rename.
#:
#: A dict rather than a chain of `if`s so the contract test can read the values back out instead
#: of pattern-matching source, and so `_router_code` provably returns nothing else
#: (`test_the_router_only_ever_answers_with_a_documented_code`).
ROUTER_CODES = {
    403: "host_not_allowed",
    404: "not_found",
    # 501, not 405: `BaseHTTPRequestHandler` answers an unknown method with NOT_IMPLEMENTED, and
    # that is the only way this code is reached. The two used to share the name
    # `method_not_allowed`, so the code said 405 while the status said 501.
    501: "method_not_implemented",
    400: "bad_request",
    # `505 HTTP Version Not Supported` is a 5xx number for a client mistake -- the base class
    # raises it on a malformed version in the request line. Listed explicitly so the catch-all
    # below does not label it `internal_error` and send someone looking for a bug in this server.
    505: "bad_request",
    500: "internal_error",
}


def _router_code(status: int) -> str:
    """A documented code for `status`. Anything unlisted collapses onto the 4xx/5xx catch-all --
    `414 URI Too Long` and `431 Header Too Large` are `bad_request`, both of which the base class
    can raise before this module sees a request at all.
    """
    if status in ROUTER_CODES:
        return ROUTER_CODES[status]
    return ROUTER_CODES[400] if status < 500 else ROUTER_CODES[500]


#: What `mimetypes` cannot be trusted to know on every machine, and what the page actually loads.
_CONTENT_TYPES = {".html": "text/html", ".css": "text/css", ".js": "text/javascript",
                  ".json": "application/json", ".mp4": "video/mp4", ".jpg": "image/jpeg",
                  ".jpeg": "image/jpeg", ".png": "image/png", ".wav": "audio/wav"}


def _is_same_file(left, right) -> bool:
    """Whether two paths name the same inode -- the filesystem's own answer, not the text's.

    `Path.resolve()` normalises `..` and symlinks but **not case**, so on a case-insensitive volume
    (APFS by default, which is what this machine runs) `<outdir>/QUEUE` and `<outdir>/queue`
    resolve to two different strings for one directory. Any comparison of names is therefore
    decided by how the *request* spelled it. `os.stat` is not.

    A path that does not exist is not the same file as anything, which is also the right answer
    for a caller asking "is this the queue".
    """
    try:
        return os.path.samefile(left, right)
    except OSError:
        return False


def _content_type(path: Path) -> str:
    guess = _CONTENT_TYPES.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0]
    guess = guess or "application/octet-stream"
    return f"{guess}; charset=utf-8" if guess.startswith("text/") else guess


def _serve_file(root, relative: str, *, suffixes) -> tuple[int, str, bytes]:
    """A file from under `root`, or a 404 -- never anything from outside `root`.

    `root` is the *leaf* directory the URL prefix maps to (`webui/` for `/static/`, one run's
    directory for `/media/`), never an ancestor of it, which is the difference between refusing
    `/static/../cli.py` and serving this project's source.

    The refusal for an escape is `path_outside_root` with a 400, not a 404. A 404 would be
    indistinguishable from a router that simply did not recognise the URL, so a traversal test
    written against it passes on a server with no path checking at all.

    `suffixes` is the allowlist of file types this route serves, and it is **required and
    keyword-only**: it used to default to `None` meaning "anything", so a future route that forgot
    the argument would serve the whole directory and say nothing. Forgetting it is now a
    `TypeError` at the call site. `/static` passes `ANY_SUFFIX` explicitly -- a decision written
    down rather than an omission -- because its root is a directory nothing writes into at run
    time. Mutation C2b is exactly the old default, which is why the class had to go and not just
    the instance.

    **The suffix is taken from `target`, the resolved path -- the same path that is then read.**
    That single fact is what defeats symbolic links: a link named `frame.mp4` pointing at
    `notes.txt` is resolved by `resolve_within` before anything looks at its name, so the suffix
    check sees `.txt` and the escape out of the root is caught by the same resolution. Taking the
    suffix from `relative` instead would check the link's name and read the target's bytes -- two
    different files, one decision. `test_the_suffix_and_the_bytes_come_from_the_same_path` pins it.

    The order is `resolve` -> `suffix` -> `is_file`, so a refusal never doubles as an answer to
    "does this file exist".

    `OSError` from `is_file`/`read_bytes` is a 404, not a crash. `pathlib` swallows `ENOENT` and
    friends but not `ENAMETOOLONG`, so a name over 255 bytes used to reach the handler's
    `internal_error` net -- reporting caller-controlled input as a bug in this server, which is the
    exact failure `resolve_within`'s own docstring calls out. It made an absurd asymmetry visible
    once the suffix check moved ahead of it: a 300-character `.json` answered 400 and a
    300-character `.mp4` answered 500.
    """
    target = resolve_within(Path(root) / relative, {"served": Path(root)}, write=False)
    if target.suffix.lower() not in suffixes:
        raise CliError(
            "media_type_not_allowed",
            f"this route serves only {sorted(suffixes)}, and {target.name!r} is none of them",
            {"path": relative, "suffix": target.suffix, "allowed": sorted(suffixes)},
        )
    try:
        if not target.is_file():
            return 404, "application/json", _error_bytes(
                "not_found", f"no such file: {relative}", {"path": relative})
        return 200, _content_type(target), target.read_bytes()
    except OSError as exc:
        # Unreadable and non-existent are one answer on purpose: the alternative distinguishes
        # "this file is here but you may not have it" from "this file is not here", which is an
        # existence oracle for anything the server can stat but not read.
        return 404, "application/json", _error_bytes(
            "not_found", f"no such file: {relative}",
            {"path": relative, "error": f"{type(exc).__name__}: {exc}"})


class _Handler(BaseHTTPRequestHandler):
    """One request. Every route returns `(status, content_type, body_bytes)`; every failure is
    funnelled through `_respond` so that a refusal looks the same whichever route raised it.
    """

    server_version = "h3-web"

    def do_GET(self) -> None:
        self._respond(self._route_get)

    def _check_host(self) -> None:
        """Refuse a request whose `Host` is not this server's own address.

        Without this, **DNS rebinding reads the whole queue today.** The design spec already states
        the threat -- "браузер выполняет чужой JavaScript, и запрос может прийти не только со своей
        страницы" -- but circle 1 of the server drew only the directory-traversal conclusion from
        it. The rest of it: a page on `evil.example` whose name resolves, on its second lookup, to
        `127.0.0.1` is same-origin with itself, so the browser sends the request and hands the
        response back to the attacker's script. Nothing about binding the loopback prevents that;
        the loopback is where the browser already is.

        `Host` is the one header the attacker cannot forge from JavaScript -- it is the name that
        was navigated to. Comparing it against the address this server was actually bound to is
        therefore the whole defence, and it is five lines.

        A missing `Host` is refused as well. HTTP/1.0 permits it, but a browser never omits it, so
        allowing it would leave the check with a hole reachable by the same `fetch` it exists to
        stop -- and a `curl` or `nc` by hand can pass one.
        """
        host = self.headers.get("Host") if self.headers else None
        if host not in self.server.allowed_hosts:
            raise CliError(
                "host_not_allowed",
                f"Host {host!r} is not this server's address; expected one of "
                f"{sorted(self.server.allowed_hosts)}",
                {"host": host, "allowed": sorted(self.server.allowed_hosts)},
            )

    def _respond(self, route) -> None:
        """Run `route` and write whatever it produced, converting every exception into JSON.

        `CliError` carries its own code, so the only decision here is the status (`ERROR_STATUS`).
        Anything else is a bug in this server rather than a refusal of the request, and becomes
        `internal_error` with the exception's *type* in `detail` -- the type, not the message,
        because the message can contain a path or a prompt and this is the one response nobody
        anticipated the contents of.
        """
        try:
            # Before the route, not inside it: every route this server will ever have -- including
            # task 6's mutating ones -- has to be behind this, and a check a route has to remember
            # to call is a check the next route forgets.
            self._check_host()
            status, content_type, body = route()
        except CliError as exc:
            status = ERROR_STATUS.get(exc.code, 400)
            content_type = "application/json"
            body = _error_bytes(exc.code, exc.message, exc.detail)
        except Exception as exc:  # noqa: BLE001 - last-resort JSON safety net, as in `cli.main`
            status = 500
            content_type = "application/json"
            body = _error_bytes("internal_error",
                                "an unexpected exception reached the HTTP boundary",
                                {"type": type(exc).__name__})
        self._send(status, content_type, body)

    def _route_get(self) -> tuple[int, str, bytes]:
        """Dispatch a GET.

        The path is `unquote`d **before** anything looks at it, so `%2e%2e%2f` and `../` are the
        same request by the time either routing or `resolve_within` sees them. Decoding afterwards
        -- routing on the raw text and unquoting only the tail -- is the classic hole: the check
        inspects one string and the filesystem opens another.
        """
        path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)

        if path == "/":
            return _serve_file(self.server.webui, "index.html", suffixes=ANY_SUFFIX)
        if path.startswith("/static/"):
            return _serve_file(self.server.webui, path[len("/static/"):],
                               suffixes=ANY_SUFFIX)
        if path == "/api/state":
            return (200, "application/json",
                    _json_bytes(build_state(self.server.queue_root, self.server.outdir)))
        if path.startswith("/media/"):
            return self._media(path[len("/media/"):])
        return 404, "application/json", _error_bytes(
            "not_found", f"no route for {path}", {"path": path})

    def _media(self, relative: str) -> tuple[int, str, bytes]:
        """A preview frame or a finished clip from **one** run's directory under the outdir.

        Four checks, and review circle 1 found the route shipping with only two of them. "Inside
        the outdir" plus "inside the run directory" reads complete and is not: the *first segment*
        of the URL decides what "the run directory" means, and three spellings make it collapse
        back onto the outdir itself, at which point the second check compares the outdir with the
        outdir and passes everything under it.

        1. there must be a run segment and a file after it;
        2. `<outdir>/<run>` must resolve inside the outdir -- this is what stops `..`;
        3. it must be a **direct child** of the outdir. `/media//x`, `/media/./x` and
           `/media/%2e/x` all resolve `<outdir>/<run>` to the outdir itself, and without this line
           they served the whole output tree, `queue/logs/*.log` and `queue/pending/*.json`
           included. Verified live before the fix, on all three spellings;
        4. it must not be the queue, compared **by inode** (`os.path.samefile`) rather than by
           path. `queue/` *is* a direct child of the outdir, so check 3 lets
           `/media/queue/pending/<id>.json` through on its own -- and comparing the resolved paths
           as text let `/media/QUEUE/...` through as well, because `Path.resolve()` does not
           canonicalise case and this machine's volume is case-insensitive. `samefile` answers the
           question the filesystem actually decides: on that same pair, path equality is `False`
           and `samefile` is `True`;
        5. and the file itself must be one of `MEDIA_SUFFIXES` (in `_serve_file`), which is what
           makes the whole set survive a directory nobody thought of -- check 4 is a denylist by
           identity, and a denylist only ever covers the names someone listed.

        **What actually stops symbolic links is none of the five.** It is `resolve_within`
        resolving the link *before* anything reads its name, so both the escape and the suffix are
        judged on the target. A link named `frame.mp4` pointing at `notes.txt` is refused as
        `.txt`, and one pointing outside the run is refused as an escape. The allowlist is policy
        layered on top of that resolution, not a substitute for it -- circle 3 checked forty
        spellings (double extensions, trailing dot and space, full-width Unicode, one-dot-leader,
        Kelvin sign, `%00` either side of the extension, a FIFO, a directory named `*.mp4`) and
        the resolution is what held. `test_the_suffix_and_the_bytes_come_from_the_same_path` pins
        the property the five checks rest on.

        The tail after the run segment is deliberately *not* flattened: a run directory has
        subdirectories of its own (`checkpoints/`), and everything below it is still inside it --
        `19-real-run/checkpoints/step05.jpg` is a legitimate preview frame.

        **Threat model: the run directory is trusted.** A *hard* link named `clip.mp4` inside it,
        pointing at `queue/pending/<id>.json` or anywhere else on the volume, is served, and
        `resolve()` cannot see it -- a hard link has no target, it *is* the file. This is accepted,
        not overlooked: creating one needs local write access inside the output directory, and
        whoever has that already has everything this route could give them. `st_nlink == 1` would
        close it and would also refuse ordinary files touched by Time Machine, `cp -c` and APFS
        clones -- false refusals on real clips, bought against an attacker who is already inside
        the perimeter. What this route defends is the *remote* caller: a browser on someone else's
        page, which can send URLs and nothing else.
        """
        run, separator, rest = relative.partition("/")
        if not separator or not rest:
            return 404, "application/json", _error_bytes(
                "not_found", "a media URL is /media/<run>/<file>", {"path": relative})
        outdir = Path(self.server.outdir).resolve()
        run_dir = resolve_within(Path(self.server.outdir) / run, {"outdir": outdir}, write=False)
        if run_dir.parent != outdir or _is_same_file(run_dir, self.server.queue_root):
            raise CliError(
                "path_outside_root",
                f"/media serves one run's directory directly under the output directory, and "
                f"{run!r} is not one",
                {"path": relative, "run": run, "resolved": str(run_dir), "outdir": str(outdir)},
            )
        return _serve_file(run_dir, rest, suffixes=MEDIA_SUFFIXES)

    def send_error(self, code, message=None, explain=None) -> None:
        """JSON, never the HTML page `BaseHTTPRequestHandler` would otherwise produce.

        This is not a formality. The base class answers an unsupported method, an over-long
        request line and an unparseable request with `error_message_format`, which is HTML -- so
        without this override the "always JSON" contract holds for every response this module
        writes and breaks on the ones it does not. A client that parses `error.code` has no way to
        know which kind it just received.
        """
        status = int(code)
        self._send(status, "application/json",
                   _error_bytes(_router_code(status), message or str(code),
                                {"status": status, "explain": explain} if explain else
                                {"status": status}))

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        """One place that writes a response, so `Content-Length` cannot be forgotten on one path."""
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # The page is polled every 20 seconds and the queue changes under it; a cached `/api/state`
        # would show a worker that stopped an hour ago.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def log_message(self, format, *args) -> None:  # noqa: A002 - signature is the base class's
        """Quiet unless `make_server(verbose=True)`. The default writes every request to stderr,
        which is useful when a human started `h3 web` and pure noise inside a test suite that
        starts a server per test.
        """
        if getattr(self.server, "verbose", False):
            super().log_message(format, *args)


class _Server(ThreadingHTTPServer):
    """`ThreadingHTTPServer` plus the four paths and the flag a handler needs.

    Threading because `/media` streams a 40 MB clip while the page keeps polling `/api/state`; a
    single-threaded server would stall the poll behind the download. `daemon_threads` so a stuck
    connection cannot keep the process alive after `h3 web` is stopped.
    """

    daemon_threads = True
    allow_reuse_address = True


def make_server(queue_root, outdir, repo=None, models=None, webui=None, port=DEFAULT_PORT,
                verbose=False) -> ThreadingHTTPServer:
    """A server bound to the loopback, ready for `serve_forever()`.

    `port=0` asks the kernel for a free one; the actual number is in `server_address[1]`, which is
    how the tests reach it without racing over a fixed port.

    `repo`, `models` and `webui` are parameters rather than module constants read at call time so a
    test can point them at a temporary tree -- but they default to the real ones, so nothing in
    production depends on a caller getting them right.
    """
    httpd = _Server((LOOPBACK, port), _Handler)
    # Built from the port the socket actually got, not from `port`: with `port=0` the kernel picks,
    # and a set built from the request would reject every request the server then received.
    bound = httpd.server_address[1]
    httpd.allowed_hosts = frozenset({f"{LOOPBACK}:{bound}", f"localhost:{bound}"})
    httpd.queue_root = Path(queue_root)
    httpd.outdir = Path(outdir)
    httpd.webui = Path(webui) if webui is not None else WEBUI_ROOT
    httpd.roots = {"repo": Path(repo) if repo is not None else REPO_ROOT,
                   "outdir": Path(outdir),
                   "models": Path(models) if models is not None else models_root()}
    httpd.verbose = verbose
    return httpd
