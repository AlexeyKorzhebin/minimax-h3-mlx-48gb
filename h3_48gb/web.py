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

import argparse
import base64
import contextlib
import errno
import fcntl
import hashlib
import io
import json
import math
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import sys
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from h3_48gb import assemble as assemble_module
from h3_48gb import project as project_module
from h3_48gb import provider
from h3_48gb import queue as q
from h3_48gb import runs as runs_module
from h3_48gb import songrun
from h3_48gb.cli import DEFAULT_CANVAS, ERROR_CODES, CliError, build_parser
from h3_48gb.project import PROJECT_KINDS
from h3_48gb.worker import WORKER_LOCK_NAME, song_job_wallclock_estimate_seconds

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

#: How long a browser may keep a file it got from `/media`, in seconds. A year -- the conventional
#: "forever" of `immutable`, and the reason it is safe here is a property of the run directory
#: rather than a guess: a preview frame carries its pass number in its own file name
#: (`...-preview-step05.jpg`) and a clip is written once, when the run ends. Nothing under a run
#: is rewritten in place, so a cache hit is always the same bytes. See `_cache_control`.
MEDIA_MAX_AGE = 31_536_000

#: One `bytes=` byte-range-spec, RFC 7233 §2.1's three spellings: `<start>-<end>`, `<start>-` and
#: `-<suffix-length>`. Deliberately does not match a comma-separated *list* of ranges -- `/media`
#: serves one clip to one `<video>` tag, never a `multipart/byteranges` response, and RFC 7233
#: §3.1 lets a server that does not implement multipart ranges answer a request it does not
#: understand as if `Range` were absent, which is the choice `_parse_range` makes for anything
#: this regex does not match at all.
_RANGE_RE = re.compile(r"\Abytes=(\d*)-(\d*)\Z")


class _RangeUnsatisfiable:
    """Sentinel `_parse_range` returns for a syntactically valid byte-range-spec this file cannot
    satisfy (RFC 7233 §2.1: a first-byte-pos at or past the end of the file, or a zero-length
    suffix against any file, or any range at all against an empty one) -- distinct from `None`
    ("no `Range`, or one this route does not understand at all -- ignore it and answer 200") so a
    caller cannot confuse "unsupported" with "out of bounds": the two answer different statuses,
    200 and 416.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "RANGE_UNSATISFIABLE"


#: The one instance `_parse_range` ever returns; compared by identity (`is`), never constructed
#: again, the same shape as `ANY_SUFFIX`.
RANGE_UNSATISFIABLE = _RangeUnsatisfiable()


def _parse_range(header: str | None, total: int) -> tuple[int, int] | _RangeUnsatisfiable | None:
    """The inclusive `(start, end)` byte range `header` (a raw `Range` header value, or `None` if
    the request had none) asks for, out of a resource `total` bytes long.

    Three outcomes, and the caller (`_Handler._range_response`) answers a different status for
    each:

    * `None` -- **answer 200 with the whole file**, exactly as if `Range` had never been sent.
      Chosen for a missing header, for anything `_RANGE_RE` does not match at all (multiple
      ranges, an unknown unit, stray characters), and for a single byte-range-spec that is itself
      syntactically invalid -- `bytes=5-3` (RFC 7233 §2.1: "invalid if the last-byte-pos value is
      present and less than the first-byte-pos"). RFC 7233 §2.1 says the whole header field must
      then be ignored, not rejected, which is the rule this function applies uniformly rather than
      picking 416 for the cases that merely looked more suspicious.
    * `RANGE_UNSATISFIABLE` -- **answer 416.** A syntactically valid byte-range-spec whose
      first-byte-pos is at or past `total` (`bytes=100000-` on a 10-byte file), or a suffix of `0`
      or more bytes than exist against an empty file (`bytes=-N` when `total == 0`) -- the request
      was well-formed, it is the file that cannot satisfy it.
    * `(start, end)` -- **answer 206** with exactly those bytes, inclusive. A `last-byte-pos` that
      names or exceeds `total` (`bytes=0-999999` on a 10-byte file) is clamped to `total - 1`,
      per RFC 7233 §2.1's "the byte range is interpreted as the remainder of the representation".

    `bytes=-0` (a suffix-length of zero) is `RANGE_UNSATISFIABLE` rather than a 200: it is
    syntactically a valid `1*DIGIT`, so §2.1's "invalid, ignore the field" rule for a malformed
    byte-range-spec does not apply, but zero bytes is nothing a 206 could honestly serve either.
    """
    if not header:
        return None
    match = _RANGE_RE.match(header.strip())
    if not match:
        return None
    start_text, end_text = match.groups()
    if not start_text and not end_text:
        return None  # `bytes=-` names neither a start nor a suffix; nothing was really asked for.
    if not start_text:
        suffix = int(end_text)
        if suffix == 0 or total == 0:
            return RANGE_UNSATISFIABLE
        return (max(0, total - suffix), total - 1)
    start = int(start_text)
    end = int(end_text) if end_text else None
    if end is not None and end < start:
        return None  # a malformed byte-range-spec: ignore the whole field, per RFC 7233 §2.1.
    if start >= total:
        return RANGE_UNSATISFIABLE
    return (start, total - 1 if end is None or end >= total else end)


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

#: Where a chat session lives, relative to the output directory: `<outdir>/chat/<id>.json`. Under
#: the outdir and not in the repository, because a session is a working note about one machine's
#: runs -- it names local files and holds half-finished text -- while `prompts/` is in git on
#: purpose. `llama.log` lands in the same directory (see `provider.LlamaLocal.ensure_up`).
CHAT_DIR = "chat"

#: Where an uploaded keyframe lands, relative to the output directory: `<outdir>/uploads/`.
#: `POST /api/uploads` (task A7) writes here and answers with a path inside it, which the page
#: then puts straight into `#image`/`#end-image` -- those two fields have always taken a path,
#: never a file, so a drag-and-drop upload only needs to land somewhere `--image`/`--end-image`
#: can already point at. Under the outdir for the same reason `CHAT_DIR` is: a dropped frame is a
#: working file on one machine, not something that belongs in the repository.
UPLOAD_DIR = "uploads"

#: What a chat session may say it was opened from. A closed list, and checked on creation: a
#: session whose `kind` the page does not know is one the page can never open again, and the honest
#: moment to say so is the moment it is written. `clip` is accepted and stored but not yet acted on
#: -- the "проекты" spec is what gives it meaning.
CHAT_SOURCE_KINDS = frozenset({"new", "prompt", "job", "clip"})

#: The keys a `source` may carry beside `kind`: which prompt file, or which job/clip id.
CHAT_SOURCE_KEYS = frozenset({"kind", "name", "id"})

#: The longest a single filename may be, in bytes. 255 on APFS, on HFS+ and on every filesystem
#: this server is plausibly run against; it is a bound on what to *refuse early*, not a portability
#: claim, and the kernel's own `ENAMETOOLONG` (`name_too_long_is_a_refusal`) still answers for
#: anything stricter. See `_chat_path` for why the check cannot be left to the kernel alone.
NAME_MAX_BYTES = 255

#: What a chat session may say its mode is -- the same closed list `h3 generate --mode` accepts,
#: and checked on creation for the same reason `CHAT_SOURCE_KINDS` is. Two readers act on this
#: string and neither can question it: the model is handed `## Context\nmode: <value>` and writes
#: a prompt for whatever it is told, and the page decides from it whether the prompt needs sound
#: sections at all. A mode nobody knows produces a plausible-looking session whose prompt cannot
#: be queued (`--mode` refuses it) and whose highlighting is wrong -- both discovered a turn later,
#: after the model has been paid for.
#:
#: `test_the_modes_a_session_may_carry_are_the_modes_the_generator_accepts` pins this to argparse's
#: own `choices`, which is the contract; this is a copy of it and copies drift.
CHAT_MODES = frozenset({"t2v", "t2va", "i2v", "flf"})

#: The mode a session is assumed to be about when it does not say. `t2va` is what `h3 generate`
#: itself defaults to, and the difference matters to the model: an `i2v` prompt opens with a
#: sentence about the given first frame, a `t2va` one must not.
DEFAULT_CHAT_MODE = "t2va"

#: The duration (seconds) a chat session is assumed to be about when neither it nor a turn says
#: otherwise -- the same 10 the page's own `#duration` field ships with (`index.html`). It reaches
#: the model as `## Context\nduration: <value> s` (`_locked_turn`), the number `analysePrompt`
#: needs to say whether a shot's cut lands inside the declared runtime; a session that has never
#: been told a duration must still give the model *some* answer rather than an absent field.
DEFAULT_CHAT_DURATION = 10

#: The longest a chat session may declare itself to be, in seconds. There is no such cap on
#: `h3 generate --duration` itself (any positive float is a real request), but the chat's own
#: `duration` is not a generation parameter: it is one line of context a person types into a
#: plain `<input type=number>` (`#chat-duration`), and its only job past that is bounding what
#: `analysePrompt`'s shot-cut check considers "past the end" of the clip. Sixty is a full minute
#: of dialogue-and-shots -- far past anything this project has ever actually generated (see
#: `prompts/`, all single-digit-to-ten-second scenes) -- and still small enough that a stray extra
#: digit (`600`, `6000`) reads as obviously wrong rather than merely large.
CHAT_DURATION_MAX = 60

#: The only file types a chat turn will attach as a keyframe. An **allowlist**, and the same shape
#: as `MEDIA_SUFFIXES` for the same reason -- with one addition that is not a formality.
#:
#: `resolve_within` bounds *where* a keyframe may live; it says nothing about *what* the file is,
#: and `<outdir>/.env` -- the file that holds this machine's provider tokens -- lives inside one of
#: those roots. Review circle 1 of this task pointed a session at it and watched the bytes leave
#: base64'd inside a chat turn, to an external provider. The bound and the type are two different
#: questions and both have to be asked.
#:
#: No `.mp4` and no `.wav`, unlike `/media`: this list is what a *vision model* is sent, not what
#: the page may display.
CHAT_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp"})

#: The biggest keyframe a turn will carry. Not a security bound -- `CHAT_IMAGE_SUFFIXES` is that --
#: but a limit on what is worth sending: base64 inflates by a third, and a 40 MB frame becomes 53 MB
#: of context that a local model spends minutes on before answering. Bigger than `MAX_BODY_BYTES`
#: on purpose: that one bounds what a *request* may carry, and a keyframe named by a *session*
#: arrives as a path, already on disk.
#:
#: `POST /api/uploads` (task A7) is the one route where a keyframe *does* arrive as a request
#: body -- a drag-and-drop file, not yet a path -- and `MAX_BODY_BYTES` still does not bound it:
#: that route never calls `_json_request` (its body is raw bytes, not JSON), so the 4 MiB JSON
#: limit is simply never in its path. What bounds the upload instead is this constant, deliberately
#: reused rather than a second, larger number invented for the route: an upload over
#: `CHAT_IMAGE_MAX_BYTES` would only stage a file `_turn_content` refuses to read the moment a
#: chat turn tries to send it, so there is no size a bigger upload limit would actually let through.
CHAT_IMAGE_MAX_BYTES = 16 * 1024 * 1024

#: Task 6 ("Проекты"): `POST /api/uploads` (task A7's route, reused rather than duplicated -- design
#: spec addendum, "импортированный трек": "mp3 загружается аплоадом ... существующий /api/uploads")
#: also accepts a finished song a person wants to import as a `kind="clip"` project's track. A
#: separate suffix set from `CHAT_IMAGE_SUFFIXES`, checked alongside it in `_upload_frame`, rather
#: than folding `.mp3` into that set: the two lists mean different things to different readers (a
#: chat turn attaches an image, never an mp3) and keeping them apart keeps `CHAT_IMAGE_SUFFIXES`
#: honest about what it actually names.
UPLOAD_AUDIO_SUFFIXES = frozenset({".mp3"})

#: The biggest track a person may import, mp3 bytes on the wire. Larger than `CHAT_IMAGE_MAX_BYTES`
#: on purpose -- a song runs minutes, not one still frame, and even a modest bitrate clears 16 MB
#: well before the "тестируется на 30-90 с" first release's own tracks would; the architecture is
#: explicitly not duration-limited (design spec, "Суть"), so this leaves headroom for longer ones
#: without pretending to be unlimited.
UPLOAD_AUDIO_MAX_BYTES = 64 * 1024 * 1024

#: The longest a sanitized upload filename may be, in bytes -- matched against `X-Filename` after
#: `sanitize_upload_name` has already stripped it to `[A-Za-z0-9._-]`. Well under `NAME_MAX_BYTES`
#: (255, the filesystem's own bound) on purpose: `_upload_target` still prefixes the sanitized name
#: with a timestamp-and-nonce stamp (`_upload_stamp`, ~22 bytes), and the two together have to fit
#: inside the same 255-byte filename the kernel enforces.
UPLOAD_NAME_MAX_BYTES = 100

#: How much of an over-long name's readable head survives `sanitize_upload_name`'s cut, in bytes.
#: Bounded separately from `UPLOAD_NAME_MAX_BYTES` rather than being "whatever the suffix leaves
#: over": sixty bytes is already more than anyone reads off a file listing, and spending the rest
#: of the allowance on staying clear of the filesystem's 255-byte limit (which the name shares with
#: `_upload_stamp`'s prefix) is worth more than forty more characters nobody reads.
UPLOAD_NAME_STEM_MAX_BYTES = 60

#: Cyrillic letters as their closest Latin spelling. A courtesy for the common case on this
#: Russian-speaking machine, not a security boundary -- `_UPLOAD_NAME_UNSAFE` runs after this and
#: would turn an untranslated «кадр.png» into the equally-safe but unreadable «----.png» on its
#: own. Bare lowercase letters only; `_transliterate_cyrillic` handles case itself, the same way a
#: human would read «Кадр» as capitalised «Kadr» rather than «KADR» or «kADR».
_CYRILLIC_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
    "я": "ya",
}

#: What survives in a sanitized upload filename, once `_transliterate_cyrillic` has already run.
#: A **replacement**, not a drop: `_UPLOAD_NAME_UNSAFE.sub("-", name)` turns every run of anything
#: else -- a slash, a space, an untranslated character -- into one `-`, so two names that differ
#: only in the replaced run do not collide into the same file, and no separator survives to be
#: read by a filesystem as structure. The character class is `PROMPT_NAME`'s own alphabet
#: (`[A-Za-z0-9_-]`) plus the dot a suffix needs.
_UPLOAD_NAME_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _transliterate_cyrillic(text: str) -> str:
    """`text` with every Cyrillic letter replaced by its closest Latin spelling.

    See `_CYRILLIC_TRANSLIT`'s docstring for why this exists and what it is not.
    """
    pieces = []
    for ch in text:
        piece = _CYRILLIC_TRANSLIT.get(ch.lower())
        if piece is None:
            pieces.append(ch)
        else:
            pieces.append(piece.capitalize() if ch.isupper() and piece else piece)
    return "".join(pieces)


def sanitize_upload_name(raw: str) -> str:
    """`raw` (an `X-Filename` header) reduced to a bare filename safe to place inside `uploads/`.

    Three steps, in order, and each answers a different failure:

    1. **basename, both slash spellings.** `X-Filename` is a header a person can set from `curl`
       on any OS, so a POSIX-only `os.path.basename` would leave a backslash-separated
       `..\\..\\x.png` untouched; both `/` and `\\` are normalised before anything splits on them.
       `../x.png` and `/etc/passwd` collapse to their last segment here, before either reaches a
       filesystem call.
    2. **transliteration** (`_transliterate_cyrillic`) -- readability, not safety.
    3. **the character filter** (`_UPLOAD_NAME_UNSAFE`) -- this is the step that actually makes
       the name safe: nothing outside `[A-Za-z0-9._-]` survives, so nothing a filesystem would
       read as a separator or a `..` segment can reach `_upload_target`. That function's own
       `resolve_within` call is the second, independent line behind this one, exactly as
       `resolve_prompt_name`'s docstring explains for `PROMPT_NAME` -- a whitelist here is not a
       reason to skip the path check there, or the other way around.

    **A name too long is shortened, never refused.** Length is a property of the name, not of the
    picture, so there is nothing here for a person to fix and nothing to tell them. Image
    generators name the file after the prompt -- two hundred characters is ordinary -- and the old
    cut took the first `UPLOAD_NAME_MAX_BYTES` bytes from the left, which ate `.png`; since
    `_upload_frame` decides "is this an image" from the suffix alone, a valid frame came back as
    `bad_image`, i.e. "кадр не годится для разговора с моделью", for having a long name.

    The cut therefore keeps two things and drops only the middle:

    * **the suffix**, because that is what makes the file an image to every later reader; and
    * **a short digest of the whole sanitized name**, because the part being dropped is exactly the
      part where prompt-named files differ. An evening's frames all begin
      `ultra_realistic_cinematic_...` and diverge at the end, so a stem-only cut collapses them
      into one name. `_upload_target`'s timestamp stamp already keeps the *path* unique, so this is
      about a human reading `uploads/` and telling two frames apart, not about collisions.

    The readable head is bounded by `UPLOAD_NAME_STEM_MAX_BYTES` rather than by whatever the suffix
    leaves over: sixty bytes is already more than anyone reads off a file listing, and the rest of
    the allowance is better spent staying well clear of the 255-byte limit the stamp shares.

    An empty result (a name that was nothing but separators and unsafe characters) becomes
    `"upload"` rather than an empty string, so `_upload_target` never has to reason about a
    sanitized name with nothing in it.
    """
    name = (raw or "").replace("\\", "/")
    name = name.rsplit("/", 1)[-1].strip()
    name = _transliterate_cyrillic(name)
    name = _UPLOAD_NAME_UNSAFE.sub("-", name).strip("-")
    if not name:
        name = "upload"
    encoded = name.encode("utf-8", "ignore")
    if len(encoded) <= UPLOAD_NAME_MAX_BYTES:
        return name

    # Not a security digest: it exists so two prompt-named frames stay distinguishable in a
    # listing. `blake2b` with a four-byte digest is eight hex characters, short enough to read.
    digest = hashlib.blake2b(encoded, digest_size=4).hexdigest()
    suffix = Path(name).suffix
    tail = f"-{digest}{suffix}"
    budget = min(UPLOAD_NAME_STEM_MAX_BYTES,
                 UPLOAD_NAME_MAX_BYTES - len(tail.encode("utf-8")))
    # `errors="ignore"` on the way back: a hard byte cut can land mid-character if a future
    # widening of `_UPLOAD_NAME_UNSAFE` ever admits a multi-byte one, and dropping the partial
    # tail is preferable to `UnicodeDecodeError` turning a 400 into a 500.
    if budget < 1:
        # The "suffix" alone eats the whole allowance, which means it is not really a suffix --
        # a name like `x.` followed by two hundred characters. Nothing here is worth preserving
        # selectively, so fall back to the plain bounded cut.
        return encoded[:UPLOAD_NAME_MAX_BYTES].decode("utf-8", "ignore").strip("-") or "upload"
    stem = Path(name).stem.encode("utf-8", "ignore")
    stem = stem[:budget].decode("utf-8", "ignore").rstrip("-.") or "upload"
    return stem + tail


def _upload_stamp() -> str:
    """`YYYYmmdd-HHMMSS-<6 hex>`: readable at a glance in a directory listing (`queue.py`'s
    `_stamp` shape), with a random tail (`queue.py`'s own `secrets`-based uniqueness) so that
    dropping both a first and a last frame inside the same second -- an ordinary `flf` upload,
    two POSTs a script or a fast double-drop can send within the same clock tick -- lands as two
    files, not one silently overwriting the other.
    """
    return f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"

#: HTTP status for each `CliError` code that is not a plain refusal of the request. Everything
#: absent from here is 400: the caller asked for something this server will not do.
#:
#: `job_not_pending` is **409**, not 400: that request was valid and lost a race with the worker
#: rather than being wrong -- the job left `pending/` between the page's last poll and this
#: request. It is mapped here *before* task 6 raises it, on purpose; see `PLANNED_CODES`.
ERROR_STATUS = {
    "host_not_allowed": 403,
    # A separate code from `host_not_allowed`, and separate on purpose: `Host` answers "which name
    # did you arrive by", `Origin` answers "which page started this", and the two need different
    # sentences from the page. A wrong `Host` is something a person can fix in the address bar; a
    # wrong `Origin` is not their doing at all, and the honest sentence is "another site pressed
    # that button". One code for both would leave the page branching on `detail` keys, which is
    # precisely the "match on the code, never on the message" contract these codes exist to keep.
    "origin_not_allowed": 403,
    "job_not_pending": 409,
    # 409 for the same reason `job_not_pending` is: the request was valid and lost a race -- with
    # another turn of the same session, which is holding its lock and talking to the model.
    "chat_busy": 409,
    # 409 for the third time on this route, and the same reason both earlier ones give: the
    # request is not wrong (400 would blame the caller) and this server is not broken (500 would
    # blame itself) -- the session file on disk is, and only a person with an editor can fix it.
    "chat_corrupt": 409,
    "queue_unwritable": 500,
    "internal_error": 500,
    # Task 6 ("Проекты"): same reasoning as `job_not_pending`/`chat_busy`/`chat_corrupt` above --
    # the request itself was fine, it is the project's own current state that refuses it.
    "project_stage_not_ready": 409,
    "project_running": 409,
    "project_scene_locked": 409,
    "project_not_found": 404,
    "project_scene_not_found": 404,
    # Established elsewhere (`_read_chat`/`_delete_chat`) as a literal `(404, ...)` tuple, never
    # routed through `CliError` before `_create_project` (task 6) became the first caller to
    # `raise` it -- added here so that raise gets the same status the tuple-returning routes
    # already answer with, rather than silently falling to 400.
    "chat_not_found": 404,
    # RFC 7233 §4.4: the request was well-formed, `Range` and all -- it is the file that cannot
    # satisfy it (a first-byte-pos at or past the file's own length). Not 400: the caller made no
    # mistake a page could tell someone to fix, and not 404: `_media` already resolved a real file
    # before this triggers.
    "range_not_satisfiable": 416,
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
#: That is exactly what happened to `job_not_pending` in task 6: the code arrived, the entry left,
#: and `ERROR_STATUS`'s 409 -- placed here a task early -- is now covered by the ordinary check.
PLANNED_CODES = frozenset()


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


def check_path_flags(args: list[str], roots: dict[str, Path]) -> list[str]:
    """`args` with every path flag's value replaced by the checked, absolute path -- or a refusal.

    **It returns the argument list, and the caller must use what it returns.** Checking one string
    and queueing another is the whole bug this shape exists to prevent, and it is not hypothetical:
    `Path.resolve()` anchors a relative value at the *current* process's working directory, which
    is the server's, while the job is run by a worker started from some other terminal. `--outdir
    out` checked here as `<server cwd>/out` would be executed as `<worker cwd>/out` -- a different
    directory, never examined by anything. `~` is the same problem in a second spelling: argparse's
    `type=Path` does not expand it, so `--outdir ~/video-out` reaches the filesystem as a literal
    directory named `~`. Both disappear once the value stored in the job is the resolved one.

    Both spellings argparse accepts are checked -- `--outdir /etc` and `--outdir=/etc` -- because
    the second one is a perfectly ordinary way to write it and a checker that only understood the
    first would be a hole with a test suite in front of it. Each is rewritten in the spelling it
    arrived in, so an argument list stays recognisable to the person who submitted it.

    A flag with no value after it is left alone: `argparse` in the validation subprocess is what
    reports that, with a better message than this function could invent.

    This is not the whole path defence for a submission, and it cannot be. `--tag` takes no path
    and yet builds one -- the output name is `outdir / f"h3-{tag}-{W}x{H}"`, so `--tag
    ../../../../tmp/pwned` walks straight through this function. The line that catches it is the
    `resolve_within` on `output_stem` in `validate_args`'s caller, which judges the path the run
    would actually write rather than the flags it was spelled with. See `prepare_submission`.
    """
    normalised = [str(item) for item in args]
    index = 0
    while index < len(normalised):
        token = normalised[index]
        flag, equals, inline = token.partition("=")
        if flag not in PATH_FLAGS:
            index += 1
            continue
        if equals:
            value, value_index = inline, index
        elif index + 1 < len(normalised):
            value, value_index = normalised[index + 1], index + 1
            index += 1
        else:
            index += 1
            continue
        resolved = resolve_within(value, roots, write=PATH_FLAGS[flag] == "write")
        normalised[value_index] = f"{flag}={resolved}" if equals else str(resolved)
        index += 1
    return normalised


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

    `paused` is `queue.is_paused(queue_root)` read fresh on every call, same as `worker`'s `state`:
    the page's pause/resume button (`POST /api/queue/pause`/`/start`) and `main_loop`'s own gate
    both act on the same marker file, and this is the one place a human sees whether either of them
    has.

    **Except when `queue_root` does not exist yet, when `paused` is `True` regardless of what
    `is_paused` says.** `is_paused`'s own contract treats a missing marker as "not paused" -- its
    deliberately safe direction, documented on that function, for a queue that *used to have* a
    marker and lost it. A queue root that has never been created at all is a different situation:
    nothing has called `queue.layout` yet (no job submitted, no worker started), and the moment
    anything does, `layout` creates it paused (see its own docstring). Passing `is_paused`'s literal
    answer through here would tell a human's very first page load "the queue is running" about a
    queue that is guaranteed to start paused the instant it exists -- review round 1 caught exactly
    this: the button would offer «⏸ Приостановить» before a single job could ever have run.

    **`outdir` is the server's own, resolved the same way `_media` resolves it.** Fix round 1
    (task A6 review): the page cannot build a `/media/<run>/<file>` link from `output_stem` alone
    once a job's own subdirectory can sit at any depth inside `--outdir` -- the C1 finding was
    exactly that `mediaParts` guessed the depth was always 1, which a job whose form-level
    `--outdir` already named a subdirectory of its own (the page's own `defaultOutdir()` default,
    `~/video-out/<date>`) breaks. The client has no other way to know where the server's own root
    ends and a job's own path begins, so it is handed over here, once, as a plain string --
    `str(Path(outdir).resolve())`, matching `_media`'s own `outdir = Path(self.server.outdir).
    resolve()` exactly, so a prefix comparison on the client is comparing the same two strings
    `_media` itself would produce.
    """
    with queue_errors(queue_root):
        jobs, broken = q.scan(queue_root)
        state = worker_state(queue_root)
        paused = True if not Path(queue_root).is_dir() else q.is_paused(queue_root)

    grouped: dict[str, list[dict]] = {name: [] for name in q.QUEUE_STATES}
    for job in jobs:
        grouped[job.state].append(job.as_dict())
    grouped["pending"].sort(key=lambda row: (-int(row.get("priority") or 0), row["id"]))

    return {
        "ok": True,
        "worker": {"state": state},
        "paused": paused,
        "outdir": str(Path(outdir).resolve()),
        "queue": {**grouped,
                  "broken": [{"path": item.path, "error": item.error} for item in broken]},
        "runs": [run.as_dict() for run in runs_module.scan(Path(outdir))],
        # I2 (fix round 1, 2026-08-19 review): reuse the one `q.scan` already done above for the
        # queue's own state, rather than `project_summary` scanning again per project (P+1 scans
        # per `/api/state` poll, every 20s from the page).
        "projects": [project_summary(proj, jobs) for proj in project_module.list_projects(outdir)],
    }


# == Projects (task 6, "Проекты") =================================================================
#
# Everything here is plain functions -- no `self`, no HTTP -- so the coverage-complete scene build
# and the queue join can be unit-tested without a server. `_Handler`'s own project routes (below)
# are thin: parse the request, call one of these, translate a `project.ProjectError`/
# `ProjectSceneBuildError` into a `CliError`.


#: `docs/h3-prompt-system.md`'s own scene-length contract ("Scenario mode": "5 to 10 seconds"),
#: reused here for a `kind="clip"` project's own scenes -- a song's *sections* have nothing to do
#: with that range (a verse can run 25 seconds, a two-line bridge three), so `build_clip_scenes`
#: has to reconcile the two: longer than `SCENE_MAX_SECONDS` is split into equal-length sub-scenes
#: sharing one prompt (a straight cut inside the same section is invisible -- one continuous shot,
#: not a scene change); shorter than `SCENE_MIN_SECONDS` is folded into a neighbouring scene rather
#: than submitted as its own too-short job. Neither rule is in the design spec -- this is task 6's
#: own decision; see the task report for the reasoning.
SCENE_MIN_SECONDS = 5.0
SCENE_MAX_SECONDS = 10.0

#: Task brief ("Проекты", task 6): an instrumental gap under this many seconds is not worth its own
#: scene -- folded into whichever sung scene borders it. At or above, it becomes its own
#: instrumental scene with a fallback prompt (`_GAP_SCENE_PROMPT`), no second LLM call.
#:
#: **Honest correction (I3, fix round 1, 2026-08-19 review): this constant's own number is not the
#: threshold a caller actually observes.** `build_clip_scenes` runs *two* fold passes in sequence --
#: this one first (`eligible=lambda seg: not seg["sung"]`, gap segments only), then an unconditional
#: second pass at `SCENE_MIN_SECONDS` (5.0) over *every* segment, sung or not. A gap between 1.5s
#: and `SCENE_MIN_SECONDS` survives the first pass as its own segment (with `_GAP_SCENE_PROMPT`) only
#: to be folded straight into a neighbour by the second pass moments later, discarding that prompt
#: same as a sub-1.5s gap would have been -- there is no code path a caller outside this module can
#: observe where a 1.5-5s gap actually keeps `_GAP_SCENE_PROMPT`. The number that actually decides
#: "does an intro gap survive as its own scene" is `SCENE_MIN_SECONDS`, not this constant. This is
#: correct behaviour, not a bug (H3 itself refuses a scene shorter than `SCENE_MIN_SECONDS`, so a
#: 1.5-5s gap could never have become a real job either way) -- but the number on this line is
#: misleading on its own, and is kept at 1.5 (rather than deleted or raised to match
#: `SCENE_MIN_SECONDS`) only because the task brief names 1.5 verbatim and a future change to
#: `SCENE_MIN_SECONDS` alone (say, if H3 ever learns to render shorter clips) would make this
#: constant meaningful again without any other code changing. See the task report ("Fix round 1")
#: for the full reasoning, and `test_build_clip_scenes_*` for the direct (non-tautological) proof of
#: what the *effective* threshold is.
GAP_SCENE_THRESHOLD_SECONDS = 1.5

#: Task brief, verbatim: the fallback prompt an instrumental gap scene gets, built from whichever
#: neighbouring scene's own prompt is available -- "инструментальная интерлюдия: <промпт предыдущей
#: сцены>, no vocals, инструментальная пауза".
_GAP_SCENE_PROMPT = "инструментальная интерлюдия: {prompt}, no vocals, инструментальная пауза"

#: How far a built scene list's total duration may drift from `track["duration"]` before
#: `build_clip_scenes` refuses rather than silently shipping an incomplete timeline. Tight --
#: unlike `assemble.DURATION_TOLERANCE_SECONDS` (ffmpeg's own real-world drift), every number this
#: function works with is pure arithmetic over `track["sections"]`'s own timestamps, so a mismatch
#: here means a bug, not measurement noise.
_COVERAGE_TOLERANCE_SECONDS = 0.05


class ProjectSceneBuildError(Exception):
    """Raised by `build_clip_scenes` when `track` is not in a shape it can build a coverage-complete
    scene list from -- no measured duration yet, or sung sections with no lyric lines to draw a
    prompt from at all. Caught at the route boundary (`_approve_project_stage`) and turned into a
    `CliError`.
    """


def _clip_raw_segments(sections: list[dict], duration: float) -> list[dict]:
    """The track's timeline as `{"start", "end", "sung", "section_index"}` chunks that already tile
    `[0, duration)` with no gaps and no overlaps.

    Built from `sections`' own `start`/`end` convention (Task 2/3, fix round 1 I1/I2): a sung
    section's own `end` is already the *next sung section's* `start`, or `duration` for the last
    one -- so consecutive sung sections leave no room between them, and the only place a genuine gap
    can appear is *before* the first sung section (an instrumental intro). A solo or an outro tag
    with no lyric lines of its own (`start=None`) is invisible to this function for the same reason:
    there is no timestamp for one in `sections`, so its span is already folded into whichever sung
    section's `end` reaches past it, not a separate chunk this function could ever detect -- see the
    task report for why this is an accepted v1 limitation, not an oversight.

    `section_index` is each known section's own position in `sections` (before this function sorts
    by `start`) -- `build_clip_scenes` needs it to look up that section's own sung lines out of
    `lyrics`, which `songrun._parse_lyrics` indexes the same way (one entry per tag *occurrence*,
    Task 2's own convention).
    """
    known = sorted(
        ((i, s) for i, s in enumerate(sections) if s.get("start") is not None),
        key=lambda pair: pair[1]["start"])
    if not known:
        return [{"start": 0.0, "end": duration, "sung": False, "section_index": None}]
    segments = []
    if known[0][1]["start"] > 0:
        segments.append({"start": 0.0, "end": known[0][1]["start"], "sung": False,
                         "section_index": None})
    for pos, (i, section) in enumerate(known):
        end = known[pos + 1][1]["start"] if pos + 1 < len(known) else duration
        segments.append({"start": section["start"], "end": end, "sung": True, "section_index": i})
    return segments


def _fold_short_segments(segments: list[dict], min_seconds: float, *,
                          eligible=lambda seg: True) -> list[dict]:
    """`segments` with every run of consecutive `eligible` segments shorter than `min_seconds`
    folded into a neighbour -- backward (extending the previous surviving segment's own `end`,
    keeping *its* other fields, prompt included) if one already exists, forward (carried onto the
    next segment's own `start`) otherwise. Never drops coverage: every second of
    `[segments[0]["start"], segments[-1]["end"])` still belongs to exactly one segment afterwards,
    only the boundaries move -- a short segment's own identity (prompt, `sung`) is what is lost,
    which is the point of "merge" rather than "keep, only shorter".
    """
    result: list[dict] = []
    carry_start = None
    for seg in segments:
        start = seg["start"] if carry_start is None else carry_start
        carry_start = None
        length = seg["end"] - start
        if eligible(seg) and length < min_seconds:
            if result:
                result[-1] = {**result[-1], "end": seg["end"]}
            else:
                carry_start = start
            continue
        result.append({**seg, "start": start})
    if carry_start is not None:
        # Every segment was short and eligible -- the whole timeline collapses into whatever is
        # left of it. M1 (fix round 1, 2026-08-19 review): `result` is *provably* still empty here,
        # not merely usually empty -- `carry_start` only survives past the loop if the very last
        # segment took the "carry forward" branch above, and that branch is only reachable while
        # `result` is empty (the moment a first segment is appended, every later short/eligible
        # segment merges backward into `result[-1]` instead, never setting `carry_start` again). A
        # dead `if result: ...` branch used to sit here for the case that can never happen;
        # removed rather than covered, since there is no input that reaches it to cover.
        result.append({**segments[-1], "start": carry_start})
    return result


def _split_long_segment(seg: dict) -> list[dict]:
    """`seg` split into `n = ceil(length / SCENE_MAX_SECONDS)` equal-length pieces, sharing `seg`'s
    own other fields (`prompt` included) -- task 6's own decision (not in the design spec): a song
    section running longer than one H3 scene allows becomes several *consecutive* scenes with one
    prompt each, not a single over-length job the pipeline would refuse outright.

    `n`'s own choice keeps every piece inside `[SCENE_MIN_SECONDS, SCENE_MAX_SECONDS]` for any
    `length > SCENE_MAX_SECONDS`: `n >= length / SCENE_MAX_SECONDS` bounds each piece
    (`length / n`) at `SCENE_MAX_SECONDS` from above, and `n < length / SCENE_MAX_SECONDS + 1`
    (`ceil`'s own definition) bounds it at `SCENE_MAX_SECONDS * length / (length + SCENE_MAX_SECONDS)`
    from below, which is already above `SCENE_MIN_SECONDS` for every `length` this is ever called
    with (`length > SCENE_MAX_SECONDS == 2 * SCENE_MIN_SECONDS`).
    """
    length = seg["end"] - seg["start"]
    n = max(1, math.ceil(length / SCENE_MAX_SECONDS))
    per = length / n
    return [{**seg, "start": seg["start"] + i * per, "end": seg["start"] + (i + 1) * per}
            for i in range(n)]


def _clip_style_block(caption: str) -> str:
    """The literal sentence `build_clip_scenes` glues, word-for-word, onto every scene's own
    prompt -- a cheap, deliberate application of `docs/h3-prompt-system.md`'s own "same words" rule
    ("Describe every character, the visual style, and the palette in *exactly the same words* in
    every single scene's prompt ... the only thing carrying identity across the cut from one clip
    into the next is ... whatever text each scene's own prompt repeats") to a `kind="clip"` project,
    which has no visual bible to draw character/palette text from at all (task report, sомнение 1).

    `caption`'s own Global Metadata section (`docs/h3-prompt-system.md`, "Song mode": "genre,
    tempo/BPM feel, key/mood, instrumentation at a glance") is the only style description this
    project has by the time scenes are built -- its own first sentence, taken literally, not
    reworded. Not a real visual bible (no character, no camera, no palette) -- see the task report
    for what Task 7 would need to do better; this is cheap insurance against scene-to-scene drift,
    not a substitute for one.
    """
    first_paragraph = next((p.strip() for p in (caption or "").split("\n\n") if p.strip()), "")
    first_line = next((line.strip() for line in first_paragraph.splitlines() if line.strip()), "")
    sentence = first_line.split(". ", 1)[0].rstrip(".").strip()
    return sentence


def _clip_section_prompt(tag: str, lines: list[str], caption: str, style_block: str) -> str:
    """A minimal, templated H3 prompt for one sung song section -- task 6's own stopgap, not a
    creative decision: see the task report for why (`docs/h3-prompt-system.md`, "Song mode":
    "`scenes` stays null [for `kind=clip`/`song`] ... a clip's scenes are built later, from the
    finished song's actual section timing" -- there is no scenario array `build_clip_scenes` could
    draw real per-scene prompts from at all, unlike a `kind="video"` project's own `scenes`).

    Assembled, without any LLM call, from what *does* exist by the time a track is approved: this
    section's own sung lines (`lines`, matched by position out of `lyrics` -- see
    `songrun.parse_lyrics`), the track's own `caption` (written for Music3, but the only
    description of mood/genre/instrumentation this project has), and `style_block` (see
    `_clip_style_block`'s own docstring), repeated identically in every call `build_clip_scenes`
    makes for the same track. Not a "visual bible" in the design spec's own sense -- no character,
    no camera vocabulary, no shot list -- see the task report for what a better v2 would need.
    """
    mood = next((line.strip() for line in (caption or "").splitlines() if line.strip()),
                "a music video")
    sung_text = " ".join(lines) if lines else (tag or "instrumental section")
    style_clause = f" Visual style, identical in every scene: {style_block}." if style_block else ""
    return (
        f"integrated_multimodal_description: [Shot 1] A music video visual for the "
        f"{tag or 'song'} section. {mood}.{style_clause} The scene visually interprets what is "
        f"being sung: \"{sung_text}\".\n\n"
        "overall_soundscape: the song's own vocals and instrumentation continue, no separate "
        "diegetic sound.\n\n"
        f"non_diegetic_music: {mood}, continuing the song's own performance."
    )


def build_clip_scenes(track: dict, *, style_block: str | None = None) -> list[dict]:
    """The coverage-complete scene list for a `kind="clip"` project, once its track is measured
    (design spec, "Клипы": "клип на песню ... сцены привязываются к секциям песни ПОСЛЕ этапа
    трека"; lyric-video-director principle: "таймлайн покрывает 0 -> длительность трека без дыр").

    Sung sections (`track["sections"]`'s own entries with a known `start`) become their own scene,
    in order, with a prompt built from that section's own lyric lines (`_clip_section_prompt`) --
    there is no scenario `scenes` array for a clip project to draw from instead (see that
    function's own docstring). The one gap the section timing can ever expose (`_clip_raw_segments`)
    -- an instrumental intro before the first sung section -- becomes its own scene when it is at
    least `GAP_SCENE_THRESHOLD_SECONDS` long, with the task brief's own fallback prompt
    (`_GAP_SCENE_PROMPT`, referencing whichever neighbouring scene's own prompt survives), or is
    folded into that neighbour when it is shorter. Every resulting scene is then folded (too short)
    or split (too long) to H3's own `[SCENE_MIN_SECONDS, SCENE_MAX_SECONDS]` range.

    **The effective gap threshold is `SCENE_MIN_SECONDS` (5s), not `GAP_SCENE_THRESHOLD_SECONDS`
    (1.5s) (I3, fix round 1, 2026-08-19 review).** See `GAP_SCENE_THRESHOLD_SECONDS`'s own docstring
    for why: the unconditional second fold pass (`SCENE_MIN_SECONDS`) runs *after* the gap-specific
    one and swallows any gap segment shorter than 5s regardless of whether it cleared 1.5s, so no
    gap under 5s ever survives as its own instrumental scene in practice.

    `style_block` (fix round 1, 2026-08-19 review -- reviewer's verdict, "дешёвая правка против
    дрейфа"): a literal sentence glued onto *every* scene's own prompt, sung and gap-fallback alike
    (`_clip_section_prompt`), the same words each time -- `docs/h3-prompt-system.md`'s "same words"
    rule for keeping a visual style from drifting scene to scene, applied here with the only
    material this project has to draw one from (see `_clip_style_block`). Defaults to
    `_clip_style_block(track["caption"])` when not given explicitly -- a caller (Task 7) that later
    grows a real visual-bible field can pass it here directly instead.

    Validates the total against `track["duration"]` (Task 3's `ffprobe` reading, **never**
    `sections[-1]["end"]` -- a lyric boundary that routinely stops short of the track's actual
    audio) within `_COVERAGE_TOLERANCE_SECONDS`, **and, since fix round 1 (M5, 2026-08-19 review),
    the shape of the timeline itself** -- the first scene must start at `0.0` and every consecutive
    pair of scenes must share an exact boundary (`prev["end"] == next["start"]`), not only the sum
    of their durations: a bug that shifts every scene by the same offset, or that overlaps one pair
    while leaving a gap in another, could sum correctly while still leaving the timeline broken, and
    the total-only check would never see it. Raises `ProjectSceneBuildError` if a track has no
    measured duration yet, or if it has sung sections but `lyrics` yields no lines for any of them
    (nothing to build a prompt from), or if either shape check above fails.

    Returns `Project.scenes`-shaped dicts (`idx`/`prompt`/`duration`/`status="pending"`/
    `job_id=None`/`clip_path=None`/`keyframe_path=None`), ready to assign to `project.scenes`.
    """
    duration = track.get("duration")
    if not isinstance(duration, (int, float)) or duration <= 0:
        raise ProjectSceneBuildError(
            f"track has no measured duration to build scenes against: {duration!r}")
    duration = float(duration)
    sections = track.get("sections") or []
    lyrics = track.get("lyrics") or ""
    caption = track.get("caption") or ""
    if style_block is None:
        style_block = _clip_style_block(caption)
    _, lines = songrun.parse_lyrics(lyrics)
    lines_by_section: dict[int, list[str]] = {}
    for section_index, text in lines:
        lines_by_section.setdefault(section_index, []).append(text)

    segments = _clip_raw_segments(sections, duration)
    if not any(seg["sung"] for seg in segments):
        # Nothing was sung at all (an entirely undersung/instrumental track) -- one scene for the
        # whole thing, built the same way a real section would be, not routed through the
        # gap-fallback template (there is no "neighbouring scene" to reference).
        segments = [{**segments[0],
                    "prompt": _clip_section_prompt("instrumental", [], caption, style_block)}]
    else:
        for seg in segments:
            if seg["sung"]:
                # M4 (fix round 1, 2026-08-19 review): the section's own name comes from
                # `track["sections"]` itself (`Project`'s own schema, task 1/2) -- `sections` is
                # already positionally aligned with `songrun.parse_lyrics`'s own `lines` (both
                # index by tag *occurrence*, task 2's convention), so re-deriving the name a second
                # time out of `parse_lyrics`'s own `section_names` return was reparsing the same
                # fact `sections[i]["name"]` already states, not a second independent source.
                tag = sections[seg["section_index"]].get("name", "") \
                    if seg["section_index"] < len(sections) else ""
                section_lines = lines_by_section.get(seg["section_index"], [])
                if not section_lines:
                    raise ProjectSceneBuildError(
                        f"section {seg['section_index']} ({tag!r}) is sung (start is known) but "
                        f"has no lyric lines to build a prompt from")
                seg["prompt"] = _clip_section_prompt(tag, section_lines, caption, style_block)
            else:
                seg["prompt"] = None  # resolved below, once the gap-threshold fold has run

        segments = _fold_short_segments(segments, GAP_SCENE_THRESHOLD_SECONDS,
                                        eligible=lambda seg: not seg["sung"])
        for i, seg in enumerate(segments):
            if not seg["sung"] and seg["prompt"] is None:
                neighbour = (segments[i + 1]["prompt"] if i + 1 < len(segments)
                            else segments[i - 1]["prompt"] if i > 0 else "")
                segments[i] = {**seg, "prompt": _GAP_SCENE_PROMPT.format(prompt=neighbour or "")}

    segments = _fold_short_segments(segments, SCENE_MIN_SECONDS)

    expanded = []
    for seg in segments:
        pieces = (_split_long_segment(seg) if seg["end"] - seg["start"] > SCENE_MAX_SECONDS
                 else [seg])
        expanded.extend(pieces)

    # M5 (fix round 1, 2026-08-19 review): the timeline's own shape, not only its total -- see the
    # function's own docstring for why a shape bug can sum correctly and still be broken.
    if expanded and abs(expanded[0]["start"] - 0.0) > _COVERAGE_TOLERANCE_SECONDS:
        raise ProjectSceneBuildError(
            f"built scenes start at {expanded[0]['start']:.3f}s, not 0.0s -- coverage is not "
            f"complete")
    for prev, nxt in zip(expanded, expanded[1:]):
        if abs(prev["end"] - nxt["start"]) > _COVERAGE_TOLERANCE_SECONDS:
            raise ProjectSceneBuildError(
                f"built scenes have a seam at {prev['end']:.3f}s/{nxt['start']:.3f}s -- coverage "
                f"is not complete")

    total = sum(seg["end"] - seg["start"] for seg in expanded)
    if abs(total - duration) > _COVERAGE_TOLERANCE_SECONDS:
        raise ProjectSceneBuildError(
            f"built scenes cover {total:.3f}s, track is {duration:.3f}s -- coverage is not "
            f"complete")

    return [{"idx": i, "prompt": seg["prompt"], "duration": seg["end"] - seg["start"],
            "status": "pending", "job_id": None, "clip_path": None, "keyframe_path": None}
            for i, seg in enumerate(expanded)]


def _project_job_by_args(jobs, project_path: Path, kind: str):
    """The pending/running `kind` job (song or assemble) whose `args` names `project_path` -- Task
    3's own M6 contract: `project.json` never records "a song/assemble job is running right now"
    (`stages.track` in particular stays exactly as it was for the whole time a song job runs, see
    `h3_48gb.worker._run_song_job`'s docstring), so the only way to answer "is one in flight" is to
    join against the queue by `--project <path>`, not by anything stored on the project itself.
    """
    target = str(Path(project_path).resolve())
    for job in jobs:
        if job.kind != kind or job.state not in ("pending", "running"):
            continue
        if "--project" in job.args:
            idx = job.args.index("--project") + 1
            if idx < len(job.args) and str(Path(job.args[idx]).resolve()) == target:
                return job
    return None


def _project_active_job(proj, jobs) -> dict | None:
    """The one queue job that best explains "what is this project doing right now", or `None` if
    nothing is in flight -- for the project list/detail routes and for the delete gate
    (`project_running`). Checked in the order a human would ask about a project: a scene actually
    running (its own `job_id` is authoritative, Task 4's C2), then a song or assemble job joined by
    `--project` (Task 3's M6, see `_project_job_by_args`) -- a project's track/assembly stage
    cannot be trusted to say "running" on its own, but at most one of the three can ever be true at
    once (script/track/scenes/assembly are sequential), so there is never a real ambiguity to break
    a tie on.

    **A fourth check, added for I1 (fix round 1, 2026-08-19 review): a project-scene job the queue
    still has pending/running that no scene in `proj.scenes` points to any more.** `_retry_project_
    scene` cancels a tail scene's own pending job before it invalidates the chain (see that route's
    own docstring), but that cancellation is best-effort -- a job the worker already claimed between
    the scan and the cancel attempt cannot be un-claimed, and `invalidate_scene_chain` still clears
    that scene's own `job_id` regardless. Once cleared, the first loop above can never find that job
    again by any scene's own bookkeeping, yet the job is still real and still writes into this
    project's own directory once it finishes -- exactly what the delete gate below exists to catch.
    Found by `assemble.parse_scene_note` alone (the job's own `note`, not anything on `proj`), the
    same way `_retry_project_scene`'s own cancellation finds its targets.
    """
    by_id = {job.id: job for job in jobs if job.state in ("pending", "running")}
    for scene in sorted(proj.scenes, key=lambda scene: scene.get("idx", 0)):
        if scene.get("status") == "running" and scene.get("job_id") in by_id:
            job = by_id[scene["job_id"]]
            return {"kind": "scene", "idx": scene["idx"], "job": job.as_dict()}
    for job in jobs:
        if job.state not in ("pending", "running"):
            continue
        parsed = assemble_module.parse_scene_note(job.note)
        if parsed is not None and parsed[0] == proj.id:
            return {"kind": "scene", "idx": parsed[1], "job": job.as_dict()}
    song_job = _project_job_by_args(jobs, proj.path, q.KIND_SONG)
    if song_job is not None:
        return {"kind": "track", "job": song_job.as_dict()}
    assemble_job = _project_job_by_args(jobs, proj.path, q.KIND_ASSEMBLE)
    if assemble_job is not None:
        return {"kind": "assembly", "job": assemble_job.as_dict()}
    return None


def project_summary(proj, jobs) -> dict:
    """One row of the project list (`GET /api/projects`, and `/api/state`'s own `"projects"` --
    design spec, "Веб": "список: название, kind, этап, прогресс сцен") -- everything the list page
    needs without loading each project's full `project.json` a second time on the detail page.

    **`jobs` is a caller-supplied, already-scanned list, not a `queue_root` this function scans
    itself (I2, fix round 1, 2026-08-19 review).** Before this fix, every call scanned the queue on
    its own -- fine for `GET /api/projects/<id>` (one project, one scan), but `/api/state` and `GET
    /api/projects` each call this once per project while listing *every* project, which turned into
    P+1 full queue scans per request (every 20s, from `/api/state`'s own poll) for what is really
    one scan's worth of information. The caller (`build_state`/`_list_projects`) now scans once and
    passes the same `jobs` list to every `project_summary` call.
    """
    scenes = proj.scenes
    return {
        "id": proj.id, "kind": proj.kind, "title": proj.title, "created_at": proj.created_at,
        "stages": dict(proj.stages),
        "scenes_total": len(scenes),
        "scenes_done": sum(1 for scene in scenes if scene.get("status") == "done"),
        "scenes_failed": any(scene.get("status") == "failed" for scene in scenes),
        "track_status": proj.track.get("status"),
        "track_undersung": bool(proj.track.get("undersung")),
        "final_path": proj.assembly.get("final_path"),
        "active_job": _project_active_job(proj, jobs),
    }


def project_is_active(proj, queue_root) -> bool:
    """Whether `proj` has a scene, a track or an assembly job in flight -- the delete gate
    (`DELETE /api/projects/<id>`, task brief: "только не-running"). A stage's own `"running"`
    status alone is not enough (Task 3's M6: `stages.track` never shows it), so this is exactly
    `project_summary`'s own `active_job` reduced to a boolean, sharing the same join.
    """
    with queue_errors(queue_root):
        jobs, _broken = q.scan(queue_root)
    return _project_active_job(proj, jobs) is not None


def _parse_args(args) -> argparse.Namespace:
    """`args` through the CLI's own parser, or `args_invalid` carrying argparse's own message.

    The parser is `build_parser()` and not a second, similar one: the numbers the estimate needs
    and the subcommand the allowlist checks have to be read the same way `h3` itself reads them,
    or the server would be validating a request that differs from the one the worker runs.

    argparse reports a bad argument list by printing usage to stderr and raising `SystemExit(2)`.
    Left alone that would leave the server's stderr full of usage text and reach the handler's
    `internal_error` net -- a 500 for what is plainly the caller's mistake. stderr is captured and
    handed back in `detail`, because argparse's sentence ("unrecognized arguments: --widht 896")
    names the actual typo and nothing this module could invent would beat it.

    `CliError` is re-raised before the `SystemExit` branch on purpose: it *is* a `SystemExit`
    subclass (see `cli.CliError`), so a broad `except SystemExit` would relabel a real refusal --
    a path outside the roots, say -- as `args_invalid` and lose its code.

    Parsing here does not import `mlx`: `build_parser` only describes flags. What would import it
    is `spec_from_args`, which this module never calls -- see the module docstring and
    `validate_args`.
    """
    args = [str(item) for item in args]
    captured = io.StringIO()
    try:
        with contextlib.redirect_stderr(captured):
            return build_parser().parse_args(args)
    except CliError:
        raise
    except SystemExit as exc:
        raise CliError(
            "args_invalid",
            f"h3 will not accept these arguments: {captured.getvalue().strip() or exc}",
            {"args": args, "stderr": captured.getvalue()},
        ) from exc


#: Where a converted checkpoint keeps the DiT's quantisation record, in the two layouts that
#: actually exist on this machine. `~/models/h3-converted` is a full pipeline directory and holds
#: it under `transformer/`; `~/models/h3-8bit` *is* a transformer directory and holds it at its
#: own root. Both are checked, in that order, so `--checkpoint` may name either -- and the
#: transformer's own file wins, because it is the DiT's bit width the peak depends on and a
#: pipeline root could one day grow a file describing something else.
QUANT_CONFIG_NAMES = ("transformer/quant_config.json", "quant_config.json")

#: What the peak costs beyond activations, by DiT bit width: the resident weights. Four bit is the
#: assumption when nothing says otherwise, because it is the smaller number -- guessing 8 would
#: quietly add ten gigabytes to every estimate made against a checkpoint whose record is missing,
#: and a warning nobody can act on is worse than none.
WEIGHTS_GB = {8: 22.1, 4: 12.1}


def quant_bits(checkpoint) -> int:
    """The DiT's bit width from the checkpoint's `quant_config.json`, or 4 when there is none.

    Unreadable and absent are the same answer deliberately: this is an estimate shown next to a
    form, and refusing to draw it because a JSON file is malformed would be a worse outcome than
    drawing the 4-bit number and being 10 GB low on an 8-bit checkpoint. The bit width is also
    recorded in the returned estimate (`bits`), so the page can say which assumption it used.
    """
    if checkpoint is None:
        return 4
    for relative in QUANT_CONFIG_NAMES:
        try:
            data = json.loads((Path(checkpoint) / relative).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        bits = data.get("bits") if isinstance(data, dict) else None
        if bits in WEIGHTS_GB:
            return int(bits)
    return 4


def canvas_comes_from_the_image(parsed) -> bool:
    """Whether this request leaves the canvas for the CLI to derive from the keyframe.

    True for exactly the shape the form's «из кадра (авто)» sends: a keyframe and neither
    `--width` nor `--height`. `resolve_canvas` treats that as "derive from the image, aspect
    intact"; with only one of the two it refuses (`partial_canvas_with_image`), and with both it
    uses them as given.

    A predicate rather than an inline condition because two callers need the same answer and must
    not drift: `_estimate_only` uses it to decide whether the formula needs a dry run first, and
    the docstring of `estimate` explains what happens when nobody asks. `parsed` is an
    `argparse.Namespace` from `_parse_args`, not an argv list -- the question is about resolved
    flags, and scanning strings for `"--width"` would miss `--width=896`.
    """
    return (getattr(parsed, "image", None) is not None
            and getattr(parsed, "width", None) is None
            and getattr(parsed, "height", None) is None)


#: Stand-in prompt for the canvas-only dry run in `_estimate_only`. Never reaches a queue, a file
#: or a model: `validate_args` builds a `RunSpec` and returns, and the only three fields read back
#: out of its report are the canvas, the duration and the step count.
_CANVAS_DRY_RUN_PROMPT = "оценка канваса"


def _argv_for_canvas_dry_run(argv: list[str], parsed) -> list[str]:
    """`argv` with a placeholder prompt added if it has none, for the canvas dry run.

    An estimate deliberately carries no prompt -- `requestEstimate` builds its arguments with
    `withPrompt: false` so that typing does not send kilobytes of text per keystroke -- while
    `generate --dry-run` refuses a request without one (`prompt_missing`), because it assembles a
    whole `RunSpec`. Left alone, those two facts cancel the feature outright: «из кадра» would
    show no estimate at all, for *any* prompt, since the prompt never reaches this route to begin
    with.

    Substituting one is honest here in a way it would not be at submission: the canvas comes from
    the keyframe's pixels, and the duration and step count from flags -- the three values
    `_estimate_only` reads back are the three a prompt cannot influence. The real prompt is still
    required by `prepare_submission`, which is where a missing one is a genuine refusal.
    """
    if parsed.prompt is not None or parsed.prompt_file is not None:
        return argv
    # After the subcommand, where `spec_from_args` expects the positional -- appending would put
    # it after a flag that takes a value and make it that flag's argument instead.
    return [argv[0], _CANVAS_DRY_RUN_PROMPT, *argv[1:]]


def estimate(args, checkpoint, *, report=None) -> dict:
    """How long this request will take and how much memory it will need, by the fitted model.

    The model is the design spec's, fitted in `docs/RESULTS.md` to the four runs of 2026-08-11::

        rows      = (5.53 + 1.641*(sec - 2.4)) * (W/16) * (H/16) + 81*sec + 820
        s_forward = 5.699e-3*rows + 2.671e-7*rows**2
        seconds   = s_forward * (steps - 1) + 36 + 7.44e-5 * W * H * sec
        peak_gb   = 9.3*rows/37657 + weights(bits)

    **The overhead term is not a rounding detail.** The forward-pass model covers diffusion only,
    and a human waits for the whole run: weights loading, text encoding, video and audio decode,
    mp4 assembly. Measured, that is 2.2 minutes at 448x288x10s and 10.2 at 896x576x15s -- it
    scales with `W*H*sec`, the video VAE's work, not with diffusion time. The constant 600 seconds
    that stood here in an earlier draft doubled the estimate for the small canvas, which is the
    one case where a crude constant is worse than no estimate at all: an eleven-minute draft
    advertised as twenty-one minutes changes what a person decides to run.

    `report` is a `generate --dry-run` report, and when it is given the canvas, the duration and
    the step count come from **it** rather than from `args`. That matters for exactly one case and
    it is the case this whole module is arranged around: with `--image` and no explicit
    `--width/--height`, the canvas is derived from the keyframe by `resolve_canvas`, which imports
    `minimax_h3_mlx.packing` and `mlx` with it. This process must never do that, so without a
    report the canvas falls back to `DEFAULT_CANVAS` -- right for the form, which posts explicit
    numbers, and approximate for a keyframe run until the subprocess has answered. On submission
    the report exists, so what gets stored on the job is the estimate for the real canvas.

    Accuracy is about ten percent (the spec measures 6.3% on the fitted point, with per-forward
    times drifting upward as swap grows), so the page must round: "≈2 ч 30 мин", never "2 ч 28".
    """
    parsed = _parse_args(args)
    if report is not None:
        width, height = (int(part) for part in str(report["canvas"]).lower().split("x", 1))
        duration = float(report["duration_seconds"])
        steps = int(report["grid_points"])
    else:
        width = int(parsed.width if parsed.width is not None else DEFAULT_CANVAS[0])
        height = int(parsed.height if parsed.height is not None else DEFAULT_CANVAS[1])
        duration = float(parsed.duration)
        steps = int(parsed.steps)

    rows = ((5.53 + 1.641 * (duration - 2.4)) * (width / 16) * (height / 16)
            + 81 * duration + 820)
    seconds_per_forward = 5.699e-3 * rows + 2.671e-7 * rows ** 2
    forwards = steps - 1
    diffusion = seconds_per_forward * forwards
    overhead = 36 + 7.44e-5 * width * height * duration
    bits = quant_bits(checkpoint)

    return {
        "forwards": forwards,
        "seconds": diffusion + overhead,
        "peak_gb": 9.3 * rows / 37657 + WEIGHTS_GB[bits],
        "diffusion_seconds": diffusion,
        "overhead_seconds": overhead,
        "seconds_per_forward": seconds_per_forward,
        "rows": rows,
        "bits": bits,
        "width": width,
        "height": height,
        "duration_seconds": duration,
        "steps": steps,
    }


#: The only subcommand this server will queue. Not a list with one element -- a list invites a
#: second entry, and the reason there is exactly one is not "we only need one": queueing `worker`
#: would have the worker start a worker inside itself, and queueing `web` a server inside the
#: server. See `_check_command_allowed`.
ALLOWED_COMMAND = "generate"

#: The methods that change something, and therefore the ones `_check_origin` guards. Named as data
#: rather than spelled out in an `if` so that adding `PATCH` one day is a change to a set and not a
#: condition somebody has to notice.
MUTATING_METHODS = frozenset({"POST", "PUT", "DELETE", "PATCH"})

#: The largest request body this server will read. Prompts are the biggest thing the page sends and
#: the longest one in `prompts/` is a few kilobytes; the limit exists so a `Content-Length` of four
#: gigabytes is a refusal rather than an allocation.
MAX_BODY_BYTES = 4 * 1024 * 1024

#: How long `generate --dry-run --json` may take. It builds a `RunSpec` and returns -- no weights,
#: no checkpoint -- so it costs a fraction of a second; with `--image` it also opens the keyframe
#: and imports `minimax_h3_mlx.packing`, which is the slow case and still seconds. The timeout is
#: here so that a subprocess wedged on an unreadable network mount cannot pin an HTTP thread for
#: ever, not because the number is expected to matter.
DRY_RUN_TIMEOUT = 120


def _check_command_allowed(parsed: argparse.Namespace) -> None:
    """Refuse anything but `h3 generate`, and refuse `--no-checkpoint`.

    Two refusals under one code because they are one rule: what may be queued. Through the API a
    `worker` job would put a worker inside the worker and a `web` job a server inside the server;
    and a job without a checkpoint loses hours to any interruption, when a queue exists precisely
    so that interrupted work continues.
    """
    if parsed.command != ALLOWED_COMMAND:
        raise CliError(
            "command_not_allowed",
            f"only `h3 {ALLOWED_COMMAND}` may be queued, and this is `h3 {parsed.command}`",
            {"command": parsed.command, "allowed": ALLOWED_COMMAND},
        )
    if getattr(parsed, "no_checkpoint", False):
        raise CliError(
            "command_not_allowed",
            "--no-checkpoint is not allowed for a queued job: without a checkpoint an interrupted "
            "run loses every hour it had already spent, and continuing interrupted work is what "
            "the queue is for",
            {"command": parsed.command, "flag": "--no-checkpoint"},
        )


def validate_args(args, python=sys.executable, timeout: float = DRY_RUN_TIMEOUT) -> dict:
    """The `generate --dry-run --json` report for `args`, or the CLI's own refusal, as a `CliError`.

    **The validation rules exist once, in the CLI, and are reached through a subprocess.** Not for
    isolation's sake: `spec_from_args` calls `resolve_canvas`, which for `--image` without an
    explicit canvas imports `minimax_h3_mlx.packing` and `mlx.core` with it, and this process sits
    resident all day next to a 36 GB generation. A second copy of the MLX stack in here is memory
    this machine does not have. Purity by construction, not by promise -- see
    `test_posting_a_job_with_an_image_never_pulls_mlx_into_the_server`.

    **`--dry-run --json` are inserted immediately after the subcommand, not appended.** Appended,
    a trailing `--` in `args` would push them past argparse's option terminator and make them
    positional; that particular list happens to fail with exit 2 rather than run anything, but the
    property "this command line cannot become a real generation" should not depend on argparse's
    handling of a corner case. Placed second, nothing in `args` can reach them: `store_true` has no
    negation and there is no `--no-dry-run`. The returned report is checked for `dry_run: true` as
    well, which is the assertion that survives someone editing this function.

    Three outcomes, three answers:

    * exit 0 -- the report, which is also the only place `output_stem` can honestly come from;
    * exit 2 -- argparse refused to parse; `args_invalid` with argparse's own stderr, which names
      the typo far better than anything this module could reconstruct;
    * any other non-zero -- the CLI printed `{"ok": false, "error": {...}}`, and that refusal is
      re-raised **with its own code**, so a geometry the CLI rejects is rejected here as
      `geometry_not_multiple_of_32` and not as some server-side paraphrase.

    `cwd` is the repository so that `-m h3_48gb` resolves from a source checkout. It is deliberately
    not a way to make relative paths work: every path flag has already been rewritten absolute by
    `check_path_flags`, because the worker's working directory is not this one.
    """
    args = [str(item) for item in args]
    if not args:
        raise CliError("args_invalid", "an empty argument list names no subcommand", {"args": args})
    for position, item in enumerate(args):
        if "\x00" in item:
            # `subprocess.run` raises `ValueError: embedded null byte` from deep inside `execve`
            # preparation, and left to itself that reaches the handler's `internal_error` net --
            # a 500 for caller-controlled input, which is exactly the failure `resolve_within`'s
            # docstring forbids and which task 5 closed for `/static`. Refused here, by name,
            # rather than by widening the `except` below: an argument with a NUL in it is not an
            # argument, and saying which one is worth more than catching the exception it causes.
            raise CliError(
                "args_invalid",
                f"argument {position} contains a NUL byte, which cannot be passed to a process",
                {"position": position, "args": args},
            )
    command = [str(python), "-m", "h3_48gb", args[0], "--dry-run", "--json", *args[1:]]

    try:
        finished = subprocess.run(command, capture_output=True, text=True, timeout=timeout,
                                  cwd=str(REPO_ROOT), stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as exc:
        raise CliError(
            "internal_error",
            f"`generate --dry-run` did not finish within {timeout} s",
            {"type": type(exc).__name__, "timeout": timeout},
        ) from exc
    except OSError as exc:
        raise CliError("internal_error", f"could not run `generate --dry-run`: {exc}",
                       {"type": type(exc).__name__}) from exc

    if finished.returncode == 2:
        raise CliError(
            "args_invalid",
            f"h3 will not accept these arguments: {finished.stderr.strip() or 'exit 2'}",
            {"args": args, "stderr": finished.stderr},
        )

    report = None
    with contextlib.suppress(ValueError):
        report = json.loads(finished.stdout)

    if finished.returncode != 0:
        error = report.get("error") if isinstance(report, dict) else None
        code = error.get("code") if isinstance(error, dict) else None
        if code in ERROR_CODES:
            raise CliError(code, error.get("message") or code, error.get("detail") or {})
        raise CliError(
            "args_invalid",
            f"`generate --dry-run` refused this request (exit {finished.returncode}): "
            f"{finished.stderr.strip() or finished.stdout.strip()}",
            {"args": args, "exit_code": finished.returncode, "stderr": finished.stderr},
        )

    if not isinstance(report, dict) or report.get("dry_run") is not True:
        # Reached only if this function built the wrong command line, so it is a bug in this
        # server rather than a refusal of the request -- and the one bug it would be is the
        # dangerous one, a subprocess that was not a dry run.
        raise CliError(
            "internal_error",
            "`generate --dry-run --json` did not answer with a dry-run report",
            {"stdout": finished.stdout[:2000]},
        )
    return report


def prompt_snapshot(argv: list[str], roots: dict[str, Path]) -> tuple[str | None, str | None]:
    """The text `--prompt-file` points at and where it came from, for `queue.submit` to snapshot.

    **The text is read from the file, never taken from the request body.** The snapshot exists so
    that the bytes a person reviewed are the bytes that run; a snapshot supplied by the caller
    alongside a different `--prompt-file` would record one prompt and run another, which is worse
    than having no snapshot at all.

    The server passes the *text*; `queue.submit` writes it and repoints `--prompt-file` at the
    copy. That split is not arbitrary -- the snapshot's path contains the job `id`, and the id does
    not exist until `submit` claims it.

    Without `--prompt-file` (a prompt typed straight into the form and passed positionally) there
    is nothing to snapshot and both halves are `None`.
    """
    parsed = _parse_args(argv)
    if parsed.prompt_file is None:
        return None, None
    path = resolve_within(parsed.prompt_file, roots, write=False)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        # `--dry-run` already read this file and would have refused; reaching here means it changed
        # underneath us between the two reads. The CLI's own code for it says exactly that.
        raise CliError("prompt_file_unreadable", f"--prompt-file could not be read: {path} ({exc})",
                       {"path": str(path)}) from exc
    repo = Path(roots["repo"]).expanduser().resolve()
    source = str(path.relative_to(repo)) if path.is_relative_to(repo) else str(path)
    return text, source


def prepare_submission(args, roots: dict[str, Path], *, python=sys.executable) -> dict:
    """Everything a job needs, in the order the design spec fixes, or the first refusal.

    parse -> only `generate`, never `--no-checkpoint` -> path flags (and their normalisation) ->
    `--dry-run` subprocess -> **the output path the run would write** -> estimate.

    The order is the point. Parsing first is what makes "is this `generate`" a question about the
    parsed subcommand rather than about `args[0]` looking right. The path flags are checked before
    a subprocess is started, so a traversal attempt never becomes a process. And the `output_stem`
    check comes *after* the dry run because only the dry run knows it: with `--image` the canvas
    that completes the name is derived from the keyframe.

    **`output_stem` is checked as a path, and that is what closes `--tag`.** `--tag` carries no
    path and is not in `PATH_FLAGS` -- it cannot be, it has no `type=Path` for the parser-sourced
    coverage test to see -- yet the output name is `outdir / f"h3-{tag}-{W}x{H}"`, so `--tag
    ../../../../tmp/pwned` builds a path outside every root and walks through `check_path_flags`
    untouched. Adding `--tag` to the flag list would fix this one flag; judging the path the run
    will actually write fixes every flag that composes a path, including ones not yet written.
    """
    _check_command_allowed(_parse_args(args))
    argv = check_path_flags(args, roots)
    report = validate_args(argv, python=python)
    resolve_within(report["output_stem"], roots, write=True)
    cost = estimate(argv, checkpoint=_parse_args(argv).checkpoint, report=report)
    prompt_text, prompt_source = prompt_snapshot(argv, roots)
    return {"args": argv, "report": report, "estimate": cost,
            "prompt_text": prompt_text, "prompt_source": prompt_source}


@contextlib.contextmanager
def name_too_long_is_a_refusal(what: str):
    """Turn `ENAMETOOLONG` into a 400 naming the input, instead of a 500 blaming the queue.

    A job id and a tag both become a filename -- `queue/pending/<id>.json` -- and a name over 255
    bytes makes the filesystem refuse. Two ways in, both reachable from outside: a `--tag` of ~240
    characters (the id is built from the tag and never truncated), and, without queueing anything
    at all, a `DELETE /api/jobs/<400 characters>`. `pathlib` swallows `ENOENT` and its friends but
    not this one, so it used to travel up to `queue_errors` and answer `queue_unwritable`, 500 --
    telling a person their queue directory is broken when it is perfectly healthy and sending them
    to check the wrong permissions.

    The code is `path_outside_root`, the same one `resolve_within` already answers for a NUL byte,
    and for the same reason its docstring gives: the caller asked for something that is not a
    usable path. Circle 3 of task 5 fixed this class in `_serve_file`; the write routes reopened
    it, because they build their paths from a different input.

    Nested **inside** `queue_errors`, not around it: the inner manager sees the `OSError` first,
    and what it raises is a `CliError`, which `queue_errors` then passes through untouched.
    """
    try:
        yield
    except OSError as exc:
        if exc.errno != errno.ENAMETOOLONG:
            raise
        raise CliError(
            "path_outside_root",
            f"{what} is too long to be a filename on this filesystem: {exc}",
            {"what": what, "errno": exc.errno},
        ) from exc


@contextlib.contextmanager
def queue_write_errors(queue_root, *, what="the request"):
    """`queue_errors` plus the three refusals that are not "the queue directory is unusable".

    Two are races with the worker or with another submission, not mistakes in the request, and
    both have to keep their own status: `job_not_pending` is 409 (the job left `pending/` between
    the page's last poll and this click) and `output_stem_conflict` is 400 with the taken name in
    `detail`, because the only useful thing to tell someone is which name to change. The third is
    a name the filesystem itself refuses -- see `name_too_long_is_a_refusal`.

    The taken name comes from `exc.output_stem`, not from a value the caller precomputed and
    passed in (task A6 removed that parameter): `submit` now raises `OutputStemConflict` against
    the *relocated* stem -- the job's own subdirectory included -- decided only once `submit` is
    actually running, under its own lock. A value the caller had ready beforehand is, at best, the
    unrelocated one `prepare_submission`'s dry run reported, which names a path that was never
    really the one in conflict.
    """
    try:
        with queue_errors(queue_root):
            with name_too_long_is_a_refusal(what):
                yield
    except q.JobNotPending as exc:
        raise CliError(
            "job_not_pending",
            f"эта задача больше не ждёт в очереди: {exc}",
            {"error": str(exc)},
        ) from exc
    except q.OutputStemConflict as exc:
        raise CliError(
            "output_stem_conflict",
            f"выходное имя уже занято: {exc}",
            {"output_stem": exc.output_stem},
        ) from exc


def _refuse_if_relocation_escapes_the_roots(queue_root, args: list[str], output_stem: str,
                                            roots: dict[str, Path]) -> None:
    """`resolve_within` the output_stem `submit` will actually relocate to -- not the flat one
    `prepare_submission`'s dry run reported -- or raise `path_outside_root`.

    Fix round 1 (I2, review round 1, Important). `queue._base_outdir` strips a directory that
    matches `queue._JOB_SUBDIR_RE` (`YYYYMMDD-HHMM-<slug>`) before nesting a fresh one under it --
    right when that directory really is a job's own subdirectory from an earlier submission, wrong
    when it is the server's *own* `--outdir`, spelled the same way by coincidence (or by a human
    promoting an old job's own subdirectory to `--outdir` by hand). Left unchecked, every job
    submitted to such a server lands one level *above* the server's own root, outside every root
    this server may write to -- `prepare_submission`'s own `resolve_within` on `output_stem` cannot
    catch this, because it runs *before* `submit` relocates anything.

    `queue_root` is `_base_outdir`'s, since fix round 2 (BACKLOG "UX-мелочи"): stripping now asks
    whether some job under `queue_root` actually used the directory, not the directory's shape
    alone, so this preview needs the same `root` the real `submit` call moments later will use to
    answer the same question the same way.

    Called **before** `submit` ever writes the job to `pending/`, not after: a check that ran after
    would also have to cancel the job it had just created, and the worker could claim it in the
    window between the two -- a real race that would leave a job in `running/` about to spawn `h3
    generate` with an `--outdir` outside every root, unstoppable by the time this noticed. Checking
    first means the job never exists at all if the relocation would have escaped.

    The exact subdirectory this previews is not necessarily the one the real `submit` call moments
    later will use -- its own clock reads a few instructions later, and could cross the minute
    boundary `queue._dir_stamp` rounds to. Harmless here: whether a path escapes every root depends
    only on the *base* directory `queue._base_outdir` decides on, which this computes identically,
    never on the minute stamp appended after it.
    """
    preview_stem = q._relocate_to_job_subdir(queue_root, list(args), str(output_stem), q._now())[1]
    resolve_within(preview_stem, roots, write=True)


def _duplicate_tag_candidates(args: list[str], output_stem: str):
    """`(args, output_stem)` pairs for `_duplicate_job` to try, in order, forever.

    The source job's own `--tag` (the CLI's default, `"run"`, if `args` has none) with `-copy`
    appended, then `-copy2`, `-copy3`, ... -- **never the untouched tag**, because the untouched
    tag means the untouched `output_stem`, and that name is always taken: by the source job
    itself, if it is still `pending`/`running` (`_stem_taken` does not exclude the id being
    duplicated the way `queue.update` excludes the id being edited), or by its own artifact
    already on disk, if it is `done`/`failed`.

    `output_stem` is `outdir / f"h3-{tag}-{W}x{H}"` (`RunSpec.output_stem`), so the new one is
    built the same way, with the new tag spliced in; the `WxH` tail is recovered from the
    source's own stem rather than re-derived from `args`, so this works the same whether the
    canvas came from `--width`/`--height` or, with `--image`, from the keyframe. If the source
    stem does not have the expected shape (hand-written test fixture, future format change), the
    suffix is appended to the whole name instead -- still unique, just not as tidy.
    """
    args = list(args)
    if "--tag" in args and args.index("--tag") + 1 < len(args):
        tag_index = args.index("--tag") + 1
        old_tag = args[tag_index]
    else:
        tag_index = None
        old_tag = "run"

    stem_path = Path(output_stem)
    prefix = f"h3-{old_tag}-"
    name = stem_path.name
    tail = name[len(prefix):] if name.startswith(prefix) else None

    attempt = 1
    while True:
        suffix = "-copy" if attempt == 1 else f"-copy{attempt}"
        new_tag = f"{old_tag}{suffix}"
        new_args = list(args)
        if tag_index is not None:
            new_args[tag_index] = new_tag
        else:
            new_args.extend(["--tag", new_tag])
        new_name = f"h3-{new_tag}-{tail}" if tail is not None else f"{name}{suffix}"
        yield new_args, str(stem_path.with_name(new_name))
        attempt += 1


#: How many `-copyN` output names `_duplicate_job` tries before giving up and answering the last
#: `output_stem_conflict` it saw -- generous, since a real queue never has more than a handful of
#: duplicates of the same job sitting around at once.
DUPLICATE_ATTEMPTS = 200


def _explicit_flag_value(args: list[str], flag: str) -> str | None:
    """The value `flag` was last given in `args`, either spelling (`--flag value` or
    `--flag=value`) -- the same "a repeated flag, last spelling wins" rule
    `queue._last_outdir_token` already applies to `--outdir`. `None` if `flag` never appears.
    """
    value = None
    for index, token in enumerate(args):
        if token == flag and index + 1 < len(args):
            value = args[index + 1]
        elif token.startswith(f"{flag}="):
            value = token[len(flag) + 1:]
    return value


def _checkpoint_dir_for_job(job) -> Path:
    """Where `job`'s own resume checkpoint would live, mirroring `RunSpec.resume_checkpoint_dir`
    (`cli.py`): an explicit `--checkpoint-dir` in the job's own `args` if it gave one, or
    `output_stem`'s own directory plus `checkpoints/` otherwise -- `resume_checkpoint_dir`'s own
    default, `self.outdir / "checkpoints"`, and `outdir` *is* `output_stem`'s parent by
    construction (`RunSpec.output_stem`).
    """
    explicit = _explicit_flag_value(job.args, "--checkpoint-dir")
    if explicit:
        return Path(explicit)
    return Path(job.output_stem).parent / "checkpoints"


def _is_relocated_job_subdir(run_dir: Path, resolved_run_dir: Path, outdir: Path) -> bool:
    """Whether `run_dir` (`Path(job.output_stem).parent`) is the job's own subdirectory that
    `queue.submit` (task A6) relocates every *new* job into, rather than a shared or date-only
    directory a job from before that feature existed was written straight into.

    Both conditions are required. The directory's own *name* must match `queue._JOB_SUBDIR_RE`
    (`YYYYMMDD-HHMM-<slug>`, the exact string `_relocate_to_job_subdir` builds) -- but the name
    alone is not proof: a human could have pointed `--outdir` at a folder that merely happens to
    look like one (`--outdir ~/out/20260101-0000-notreal`) before this feature existed, and a job
    written by *that* invocation must not have its neighbours inside `~/out` treated as if they
    were this job's own subdirectory's exclusive contents. Requiring `resolved_run_dir != outdir`
    is what refuses exactly that coincidence: it never calls the server's own outdir itself a
    job's subdirectory, no matter what its name happens to match.
    """
    return q._JOB_SUBDIR_RE.fullmatch(run_dir.name) is not None and resolved_run_dir != outdir


def _delete_flat_artifacts(stem: Path, job) -> None:
    """Remove only the files a flat (pre-A6, or hand-pointed at a shared/date folder) job's own
    `output_stem` names -- never the directory around them, which this job does not own alone.

    `<stem>.mp4`, `.wav` and `.json` (the run's own report, `cli.py`'s `run_generate`) are every
    suffix a completed or half-finished run writes *directly* named after its stem, plus
    `-raw.npz` (the video+audio arrays `run_generate` saves just before the two encoders run, and
    only under `--keep-raw` -- the page never passes it, so this one is usually already absent;
    `missing_ok` covers both the old runs that have it and the new ones that do not) and the whole
    `-preview-stepNN.jpg` family (`preview.py`'s `preview_path`), globbed because there is one per
    interval rather than one fixed name.

    **The checkpoint is the one artifact `output_stem` does not name at all.** `checkpoint.py`
    names a resume file after the *request's identity digest*, not the output stem, precisely so
    two differently-tagged runs of the same prompt/seed/geometry can share one resume file rather
    than each keeping a redundant copy (`checkpoint.request_identity`). Computing that digest here
    would need a loaded model (`identity_digest` needs `pipe.checkpoint_identity_extra()`), which
    this server does not have and must not load just to answer a delete click. Instead: only when
    the job's own checkpoint directory (`_checkpoint_dir_for_job`) holds **exactly one**
    `h3-*.safetensors` file is it safe to call that file this job's -- a lone leftover in a
    directory only this job (as far as this server can tell) could have written a checkpoint into.
    Two or more is ambiguous -- which of several old, unrelated jobs left which file cannot be
    told apart without the digest -- and this function leaves all of them rather than guess. A
    checkpoint whose companion lock file (`runs_module._LOCK_SUFFIX`) proves a writer still holds
    it is left alone too, on the same "never guess, never touch what might still be live"
    principle, however unlikely a live writer is for a directory only a *finished* job's flat
    layout would still be pointing at.

    (In practice a checkpoint only survives a *successful* run's own clean-up when
    `--keep-checkpoint` was passed -- `checkpoint.py`'s `CheckpointingPipeline.__call__` discards
    it on every ordinary success -- so what this usually finds, if anything, is the one checkpoint
    a *failed* run left behind.)
    """
    for suffix in (".mp4", ".wav", ".json"):
        Path(f"{stem}{suffix}").unlink(missing_ok=True)
    Path(f"{stem}-raw.npz").unlink(missing_ok=True)
    for shot in stem.parent.glob(f"{stem.name}-preview-step*.jpg"):
        shot.unlink(missing_ok=True)

    checkpoint_dir = _checkpoint_dir_for_job(job)
    if not checkpoint_dir.is_dir():
        return
    checkpoints = sorted(checkpoint_dir.glob("h3-*.safetensors"))
    if len(checkpoints) != 1:
        return
    checkpoint = checkpoints[0]
    if runs_module._writer_alive(checkpoint) is True:
        return
    checkpoint.unlink(missing_ok=True)
    checkpoint.with_name(checkpoint.name + runs_module._LOCK_SUFFIX).unlink(missing_ok=True)


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


def _is_within(path, other) -> bool:
    """Whether `path` *is* `other`, or sits anywhere inside it -- checked level by level with
    `_is_same_file`, never by comparing resolved path text.

    Fix round 1 (task A6 review, C1): `/media` used to bound "the queue" by comparing `path`
    against `other` directly, which was enough when a served directory was always exactly one
    level under the outdir (`path` could only ever *be* the queue root, never something inside
    it). Now that a served directory can sit at any depth (a job's own subdirectory nested inside
    whatever `--outdir` the form already named), `queue/logs/x.jpg` has to be refused exactly as
    `queue` itself already was -- so every ancestor of `path`, not just `path` itself, is checked.

    Text comparison (`is_relative_to` on two resolved paths) would be wrong for the same reason
    `_is_same_file` exists at all: `Path.resolve()` does not canonicalise case, so `<outdir>/QUEUE`
    and `<outdir>/queue` are different *strings* on a case-insensitive volume even though the
    filesystem calls them the same directory. `_is_same_file` at every level is what actually
    answers "is this the queue's own file", the way the filesystem itself would.

    Bounded by path depth: `.parent` strictly shortens `path` until it reaches the filesystem
    root, where `.parent` returns itself -- the loop's own termination condition.
    """
    current = Path(path)
    while True:
        if _is_same_file(current, other):
            return True
        parent = current.parent
        if parent == current:
            return False
        current = parent


def _reveal_in_finder(path: Path) -> None:
    """Select `path` in Finder -- the default `self.server.reveal` (`_reveal_job`'s own seam,
    `make_server(..., reveal=...)`). macOS-only, and only ever called with a path already checked
    to sit inside the outdir.

    `check=False`: by the time this runs, Finder is the only thing left that can react to `path`
    no longer being there (removed between the check in `_reveal_job` and this call, or `open`
    itself missing on a non-macOS box this server should not otherwise be running on) -- and
    that reaction is not a `CalledProcessError` this process should raise on. The request already
    found a real file; a `open -R` that then fails is a cosmetic miss, not a 500.
    """
    subprocess.run(["open", "-R", str(path)], check=False)


def _content_type(path: Path) -> str:
    guess = _CONTENT_TYPES.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0]
    guess = guess or "application/octet-stream"
    return f"{guess}; charset=utf-8" if guess.startswith("text/") else guess


def _resolve_servable(root, relative: str, *, suffixes) -> Path | None:
    """`relative` resolved under `root` and checked against `suffixes`, or `None` if there is no
    such *readable file* there -- factored out of `_serve_file` so a caller that wants the file's
    own path without paying for `read_bytes()` on the whole thing (`_media`'s Range support:
    answering Safari's two-byte probe must not mean reading a 40 MB clip into memory first) can
    reuse the exact same resolve/suffix/existence policy `_serve_file` already enforces.

    `suffixes` is the allowlist of file types this route serves, and it is **required and
    keyword-only**: it used to default to `None` meaning "anything", so a future route that forgot
    the argument would serve the whole directory and say nothing. Forgetting it is now a
    `TypeError` at the call site. `/static` passes `ANY_SUFFIX` explicitly -- a decision written
    down rather than an omission -- because its root is a directory nothing writes into at run
    time. Mutation C2b is exactly the old default, which is why the class had to go and not just
    the instance.

    **The suffix is taken from `target`, the resolved path -- the same path a caller then reads.**
    That single fact is what defeats symbolic links: a link named `frame.mp4` pointing at
    `notes.txt` is resolved by `resolve_within` before anything looks at its name, so the suffix
    check sees `.txt` and the escape out of the root is caught by the same resolution. Taking the
    suffix from `relative` instead would check the link's name and read the target's bytes -- two
    different files, one decision. `test_the_suffix_and_the_bytes_come_from_the_same_path` pins it.

    The order is `resolve` -> `suffix` -> `is_file`, so a refusal never doubles as an answer to
    "does this file exist". A refusal for an escape is still `path_outside_root` (raised by
    `resolve_within`) or `media_type_not_allowed` (raised here); only "the file is not there or
    cannot be read" collapses to `None`, so a caller can turn that into its own 404.

    `OSError` from `is_file` becomes `None`, not a crash. `pathlib` swallows `ENOENT` and friends
    but not `ENAMETOOLONG`, so a name over 255 bytes used to reach the handler's `internal_error`
    net -- reporting caller-controlled input as a bug in this server, which is the exact failure
    `resolve_within`'s own docstring calls out. It made an absurd asymmetry visible once the suffix
    check moved ahead of it: a 300-character `.json` answered 400 and a 300-character `.mp4`
    answered 500.
    """
    target = resolve_within(Path(root) / relative, {"served": Path(root)}, write=False)
    if target.suffix.lower() not in suffixes:
        raise CliError(
            "media_type_not_allowed",
            f"this route serves only {sorted(suffixes)}, and {target.name!r} is none of them",
            {"path": relative, "suffix": target.suffix, "allowed": sorted(suffixes)},
        )
    try:
        exists = target.is_file()
    except OSError:
        return None
    return target if exists else None


def _serve_file(root, relative: str, *, suffixes) -> tuple[int, str, bytes]:
    """A file from under `root`, or a 404 -- never anything from outside `root`.

    `root` is the *leaf* directory the URL prefix maps to (`webui/` for `/static/`, one run's
    directory for `/media/`), never an ancestor of it, which is the difference between refusing
    `/static/../cli.py` and serving this project's source.

    The refusal for an escape is `path_outside_root` with a 400, not a 404. A 404 would be
    indistinguishable from a router that simply did not recognise the URL, so a traversal test
    written against it passes on a server with no path checking at all.

    Path policy and the allowlist both live in `_resolve_servable`; this wrapper only adds the
    whole-file read `_media`'s Range support (`_Handler._range_response`) no longer wants paid on
    every request.
    """
    target = _resolve_servable(root, relative, suffixes=suffixes)
    if target is None:
        return 404, "application/json", _error_bytes(
            "not_found", f"no such file: {relative}", {"path": relative})
    try:
        return 200, _content_type(target), target.read_bytes()
    except OSError as exc:
        # Unreadable and non-existent are one answer on purpose: the alternative distinguishes
        # "this file is here but you may not have it" from "this file is not here", which is an
        # existence oracle for anything the server can stat but not read.
        return 404, "application/json", _error_bytes(
            "not_found", f"no such file: {relative}",
            {"path": relative, "error": f"{type(exc).__name__}: {exc}"})


def _check_session_shape(sid: str, path, session) -> None:
    """Refuse a session file this server did not write, by code, before anything indexes it.

    A session is JSON in a directory a person can see (`<outdir>/chat/`), next to `llama.log`, and
    people edit what they can see -- trimming a transcript by hand is the obvious way to shorten a
    context that has grown too long. Every field but `messages` is already read through `.get`
    with a default; `messages` was indexed directly, so a file missing the key raised `KeyError`
    inside the route, reached the last-resort net at the HTTP boundary and left the page with
    `internal_error` 500 «сервер споткнулся» -- a sentence that blames this server for a file it
    did not write, and hides the one instruction that fixes it.

    The whole shape is checked, not only the missing key: a `messages` that is a string indexes
    into characters (`TypeError`), a root that is a list has no `.get` (`AttributeError`), and an
    entry without `content` is a `KeyError` one line later. All four are the same event -- the file
    is not a session -- and reporting them as one code is what lets the page say so.

    **409, not 400 and not 500.** The request was valid and this server is not broken; the
    resource on disk is, which is exactly what `chat_busy` already means by 409 on this route.
    """
    if not isinstance(session, dict):
        raise CliError("chat_corrupt",
                       f"файл сессии повреждён: {path} — в корне {type(session).__name__}, "
                       f"а должен быть объект",
                       {"id": sid, "path": str(path), "found": type(session).__name__})
    messages = session.get("messages")
    if not isinstance(messages, list) or not all(
            isinstance(m, dict) and isinstance(m.get("role"), str)
            and isinstance(m.get("content"), str) for m in messages):
        raise CliError("chat_corrupt",
                       f"файл сессии повреждён: {path} — `messages` должен быть списком реплик "
                       f"{{role, content}}",
                       {"id": sid, "path": str(path)})


@contextlib.contextmanager
def chat_session_lock(path):
    """Hold `<sid>.lock` for one turn, or refuse with `chat_busy`.

    A turn is a read-modify-write of the session file with a call to a language model in the
    middle of it, and `ThreadingHTTPServer` runs two of them in two threads. Two tabs -- or one
    double click -- therefore both read the session before either writes, both pay for the model,
    and the second write silently erases the first exchange.

    It is worse than lost text. `queue.write_text_durably` names its temp file after the **pid**,
    not the thread, so two turns of one process collide on a single `.tmp-<pid>`: whichever loses
    the `os.replace` gets `FileNotFoundError` cleaning up a file the other one already renamed --
    a 500 handed to the caller *after* the model was called and paid for. Review circle 1
    reproduced exactly that, `[200, 500]`.

    **Non-blocking, unlike `queue.queue_lock`.** That one waits because it guards milliseconds of
    file I/O; this one guards a minute of a model thinking, and a request that waits a minute to
    then do the same work is not a queue anyone asked for. `LOCK_NB` turns the second turn into an
    immediate, honest 409 -- and, because the lock is taken before the provider is called, into one
    that costs nothing.

    The lock file sits beside the session (`<sid>.lock`, never `<sid>.json`), so the lock survives
    the `os.replace` that swaps the session file underneath it: `flock` follows the inode, and
    locking the file being replaced would leave the two turns holding locks on two different
    inodes.
    """
    lock_file = Path(path).with_suffix(".lock")
    fd = os.open(lock_file, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CliError("chat_busy", "ход уже идёт — дождитесь ответа модели",
                           {"id": Path(path).stem}) from exc
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _generation_running(queue_root):
    """The jobs a worker is running right now -- empty when the GPU is free.

    A module-level function rather than a line inside the chat route for two reasons. It is the
    single place that decides what "the GPU is busy" means, so the rule cannot drift between the
    route that refuses a turn and whatever later asks the same question; and a test can replace it,
    which is the only way to exercise the refusal without starting a twenty-minute generation.

    `reconcile` rather than `scan`: a job left in `running/` by a worker that died is not a live
    generation, and only reconciliation can tell the two apart (it is what checks the lease).
    """
    return q.reconcile(queue_root).alive


def _running_ids(running) -> list[str]:
    """Job ids out of whatever `_generation_running` returned, for an error's `detail`.

    `detail` is JSON on the wire, and `reconcile().alive` is a list of `Job` dataclasses, which
    `json.dumps` cannot take. Written to survive anything iterable so that the refusal itself
    cannot be the thing that raises.
    """
    return [str(getattr(job, "id", job)) for job in running]


class _Handler(BaseHTTPRequestHandler):
    """One request. Every route returns `(status, content_type, body_bytes)`; every failure is
    funnelled through `_respond` so that a refusal looks the same whichever route raised it.
    """

    server_version = "h3-web"

    #: Seconds a connection may sit open without sending a byte before this handler gives up on it
    #: (BACKLOG "UX-мелочи", task 5). `socketserver.StreamRequestHandler.setup` reads this class
    #: attribute and calls `self.connection.settimeout(timeout)` on its own -- nothing here has to
    #: touch the socket directly -- and `BaseHTTPRequestHandler.handle_one_request` already catches
    #: the resulting `socket.timeout` and sets `close_connection = True`, so setting this attribute
    #: is the entire fix. Without it, a client that opens a connection and never sends anything
    #: (slow-loris, or just a stalled network path) ties up a handler thread forever:
    #: `daemon_threads=True` on `_Server` only means the *process* can still exit, not that a
    #: leaked thread is ever reclaimed while `h3 web` keeps running.
    #:
    #: No long-poll route exists here to conflict with a read timeout -- `/api/state` and friends
    #: are all polled by the page on its own interval, never held open waiting for a server-side
    #: event -- so a generous-but-finite number is free to pick without starving anything real.
    #: 60 s is far longer than any route here takes to answer (`DRY_RUN_TIMEOUT` alone is on the
    #: order of seconds) and far shorter than "forever".
    timeout = 60

    def do_GET(self) -> None:
        self._respond(self._route_get)

    def do_POST(self) -> None:
        self._respond(self._route_post)

    def do_PUT(self) -> None:
        self._respond(self._route_put)

    def do_DELETE(self) -> None:
        self._respond(self._route_delete)

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
        host = self._sole_header("Host", "host_not_allowed")
        if host not in self.server.allowed_hosts:
            raise CliError(
                "host_not_allowed",
                f"Host {host!r} is not this server's address; expected one of "
                f"{sorted(self.server.allowed_hosts)}",
                {"host": host, "allowed": sorted(self.server.allowed_hosts)},
            )

    def _sole_header(self, name: str, code: str) -> str | None:
        """The one value of `name`, `None` if it is absent, or a refusal if it arrived **twice**.

        A repeated header is where a check that reads "the" value quietly stops being a check.
        `email.message.Message.get` returns the *first* occurrence, so `Origin: <this page>`
        followed by `Origin: http://evil.example` passed the comparison while the second line sat
        in the same request -- and which of the two a proxy, a log or the next reader believes is
        not something this server gets to decide. No browser sends two, so refusing costs nothing
        real; picking one silently costs the whole check.
        """
        values = self.headers.get_all(name) if self.headers else None
        if not values:
            return None
        if len(values) != 1:
            raise CliError(
                code,
                f"the request carries {len(values)} {name} headers; exactly one is allowed",
                {"header": name, "count": len(values), "values": list(values)},
            )
        return values[0]

    def _check_origin(self) -> None:
        """Refuse a **write** that another site's page asked for. Reads do not need this.

        `Host` is not enough here, and that gap is the reason this exists. `Host` says which
        address was navigated to; a cross-site form posting to `http://127.0.0.1:8765/api/jobs`
        sends exactly the right one. What the browser adds, and a page cannot forge, is where the
        request came *from*: `Origin` (sent on every write, same-origin included) and
        `Sec-Fetch-Site` (sent by every current browser on every request).

        Both are checked, and each on its own terms: a header that is present must say this page,
        a header that is absent proves nothing either way. Absent *both* means the caller is not a
        browser -- `curl`, a script, a test -- and there is no cross-site request to forge there;
        refusing that case would break scripting this queue from a terminal without closing
        anything, because no browser omits both.

        `Sec-Fetch-Site: none` is a user-typed URL or a bookmark, which is this machine's owner.

        The code is `origin_not_allowed`, **not** `host_not_allowed`, and the split is deliberate.
        The two failures need different sentences from the page: a wrong `Host` is an address the
        person typed and can retype, while a wrong `Origin` is not their doing at all -- the honest
        sentence is "another site pressed that button". Sharing one code would leave the page
        branching on `detail` keys to tell them apart, which is exactly the "match on the code,
        never on the message" contract the codes exist to keep.
        """
        site = self._sole_header("Sec-Fetch-Site", "origin_not_allowed")
        if site is not None and site.strip().lower() not in ("same-origin", "none"):
            raise CliError(
                "origin_not_allowed",
                f"a write must come from this server's own page; Sec-Fetch-Site is {site!r}",
                {"sec_fetch_site": site, "method": self.command},
            )
        origin = self._sole_header("Origin", "origin_not_allowed")
        if origin is not None and origin.strip().lower() not in self.server.allowed_origins:
            raise CliError(
                "origin_not_allowed",
                f"a write must come from this server's own page; Origin {origin!r} is not one of "
                f"{sorted(self.server.allowed_origins)}",
                {"origin": origin, "allowed": sorted(self.server.allowed_origins),
                 "method": self.command},
            )

    def _respond(self, route) -> None:
        """Run `route` and write whatever it produced, converting every exception into JSON.

        `CliError` carries its own code, so the only decision here is the status (`ERROR_STATUS`).
        Anything else is a bug in this server rather than a refusal of the request, and becomes
        `internal_error` with the exception's *type* in `detail` -- the type, not the message,
        because the message can contain a path or a prompt and this is the one response nobody
        anticipated the contents of.

        `self._extra_headers` is reset here, to an empty dict, on **every** request -- not only
        once per connection. `ThreadingHTTPServer` reuses one `_Handler` instance across every
        request a keep-alive connection sends (`handle_one_request` loops), so a `Content-Range`
        `_media` set answering request 1's `Range` probe would otherwise still be sitting on
        `self` when request 2, an unrelated `/api/state` poll on the same socket, reaches `_send`.
        Resetting before `route()` runs, rather than only when `_media` itself is about to set one,
        also means a route that raises before ever touching the dict still gets a clean one.
        """
        self._extra_headers: dict[str, str] = {}
        try:
            # Before the route, not inside it: every route this server has -- the mutating ones
            # included -- is behind these two, and a check a route has to remember to call is a
            # check the next route forgets. `do_POST` and friends exist only as one-line calls to
            # this method for the same reason: a method handler written *beside* `_respond` rather
            # than through it would have opened writes to DNS rebinding, which is where the five
            # 501 answers of task 5 sat.
            self._check_host()
            if self.command in MUTATING_METHODS:
                self._check_origin()
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
        if path == "/api/projects":
            return self._list_projects()
        if path.startswith("/api/projects/"):
            return self._read_project(path[len("/api/projects/"):])
        if path == "/api/prompts":
            return self._list_prompts()
        if path.startswith("/api/prompts/"):
            return self._read_prompt(path[len("/api/prompts/"):])
        if path == "/api/providers":
            return self._providers()
        if path == "/api/llm":
            return self._llm_status()
        if path.startswith("/api/chat/"):
            return self._read_chat(path[len("/api/chat/"):])
        if path.startswith("/media/"):
            return self._media(path[len("/media/"):])
        return 404, "application/json", _error_bytes(
            "not_found", f"no route for {path}", {"path": path})

    def _route_post(self) -> tuple[int, str, bytes]:
        """Dispatch a POST. `unquote` first, for the same reason `_route_get` does it."""
        path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)

        if path == "/api/jobs":
            return self._submit_job()
        if path == "/api/estimate":
            return self._estimate_only()
        if path.startswith("/api/jobs/") and path.endswith("/top"):
            return self._promote_job(path[len("/api/jobs/"):-len("/top")])
        if path.startswith("/api/jobs/") and path.endswith("/duplicate"):
            return self._duplicate_job(path[len("/api/jobs/"):-len("/duplicate")])
        if path.startswith("/api/jobs/") and path.endswith("/reveal"):
            return self._reveal_job(path[len("/api/jobs/"):-len("/reveal")])
        if path == "/api/chat":
            return self._create_chat()
        if path == "/api/llm/unload":
            return self._llm_unload()
        if path == "/api/queue/pause":
            return self._queue_pause()
        if path == "/api/queue/start":
            return self._queue_start()
        if path.startswith("/api/chat/") and path.endswith("/message"):
            return self._chat_message(path[len("/api/chat/"):-len("/message")])
        if path == "/api/uploads":
            return self._upload_frame()
        if path == "/api/projects":
            return self._create_project()
        if path.startswith("/api/projects/"):
            # Task 6 ("Проекты"), extended by task 7's own `/track/retry`: nested action routes
            # under one project id, told apart by shape rather than one `startswith`/`endswith`
            # pair each -- `/approve/<stage>` (3 segments, second is "approve"), `/assembly/retry`
            # and `/track/retry` (3 segments, second is "assembly"/"track", third "retry"),
            # `/scenes/<idx>/retry` (4 segments, "scenes"/.../"retry").
            parts = path[len("/api/projects/"):].split("/")
            if len(parts) == 3 and parts[1] == "approve":
                return self._approve_project_stage(parts[0], parts[2])
            if len(parts) == 3 and parts[1] == "assembly" and parts[2] == "retry":
                return self._retry_project_assembly(parts[0])
            if len(parts) == 3 and parts[1] == "track" and parts[2] == "retry":
                return self._retry_project_track(parts[0])
            if len(parts) == 4 and parts[1] == "scenes" and parts[3] == "retry":
                return self._retry_project_scene(parts[0], parts[2])
        return 404, "application/json", _error_bytes(
            "not_found", f"no route for POST {path}", {"path": path})

    def _route_put(self) -> tuple[int, str, bytes]:
        path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)

        if path.startswith("/api/jobs/"):
            return self._edit_job(path[len("/api/jobs/"):])
        if path.startswith("/api/prompts/"):
            return self._save_prompt(path[len("/api/prompts/"):])
        return 404, "application/json", _error_bytes(
            "not_found", f"no route for PUT {path}", {"path": path})

    def _route_delete(self) -> tuple[int, str, bytes]:
        path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)

        if path.startswith("/api/jobs/"):
            return self._cancel_job(path[len("/api/jobs/"):])
        if path.startswith("/api/chat/"):
            return self._delete_chat(path[len("/api/chat/"):])
        if path.startswith("/api/projects/"):
            return self._delete_project(path[len("/api/projects/"):])
        return 404, "application/json", _error_bytes(
            "not_found", f"no route for DELETE {path}", {"path": path})

    # -- request bodies ---------------------------------------------------------------------

    def _json_request(self, *, allowed: tuple[str, ...]) -> dict:
        """The request body as one JSON object with only the keys `allowed`, or a refusal.

        A body arrives from outside, so a truncated one, a wrong `Content-Length` and a list where
        an object belongs are all the caller's mistakes and answer 400. Only a bug in this module
        should ever reach the `internal_error` net.

        **An unrecognised key is refused, not ignored,** and `allowed` is required and keyword-only
        so a route cannot forget to say what it takes. Silently dropping a field is the same defect
        as `suffixes=None` meaning "serve anything": whoever wrote the caller sends `prompt_source`
        or `prompt_text`, is answered 200, and goes away believing the server used it. The snapshot
        this server writes comes from the file `--prompt-file` names and from nowhere else -- a
        prompt supplied beside a *different* flag would record one text and run another -- and the
        way to say that is to refuse the field, not to swallow it.
        """
        raw_length = self.headers.get("Content-Length") if self.headers else None
        try:
            length = int(raw_length or 0)
        except ValueError as exc:
            raise CliError("bad_request", f"Content-Length is not a number: {raw_length!r}",
                           {"content_length": raw_length}) from exc
        if length < 0 or length > MAX_BODY_BYTES:
            raise CliError("bad_request",
                           f"a request body of {length} bytes is over the {MAX_BODY_BYTES} limit",
                           {"content_length": length, "limit": MAX_BODY_BYTES})
        body = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except (ValueError, UnicodeDecodeError) as exc:
            raise CliError("bad_request", f"the request body is not JSON: {exc}",
                           {"bytes": length}) from exc
        if not isinstance(payload, dict):
            raise CliError("bad_request",
                           f"the request body must be a JSON object, not {type(payload).__name__}",
                           {"type": type(payload).__name__})
        unknown = sorted(set(payload) - set(allowed))
        if unknown:
            raise CliError(
                "args_invalid",
                f"this route takes {sorted(allowed)} and nothing else; it was also sent {unknown}",
                {"unknown": unknown, "allowed": sorted(allowed)},
            )
        return payload

    @staticmethod
    def _args_of(payload: dict) -> list[str]:
        args = payload.get("args")
        if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
            raise CliError("args_invalid", "`args` must be a list of strings",
                           {"args": args if isinstance(args, (list, str)) else None,
                            "type": type(args).__name__})
        return args

    @staticmethod
    def _note_of(payload: dict) -> str:
        note = payload.get("note") or ""
        if not isinstance(note, str):
            raise CliError("args_invalid", "`note` must be a string",
                           {"type": type(note).__name__})
        return note

    def _job_id_of(self, raw: str) -> str:
        """A job id that names a file directly inside `pending/`, or `path_outside_root`.

        The id comes out of a URL and is turned into a path by `queue.job_path`, so it is a path
        component in everything but name: `../../../etc/passwd` and `a/b` both have to be refused
        before `cancel` unlinks anything. Checked the way every other path in this module is
        checked -- resolve it, then require the result to be exactly `<queue>/pending/<id>.json` --
        rather than with a private pattern for ids, because the pattern would have to be kept in
        step with `queue`'s id format and this does not.
        """
        queue_root = Path(self.server.queue_root)
        target = resolve_within(q.job_path(queue_root, raw, "pending"),
                                {"queue": queue_root}, write=True)
        if target.parent != (queue_root / "pending").expanduser().resolve() \
                or target.suffix != ".json":
            raise CliError(
                "path_outside_root",
                f"a job id names one file in the queue's pending directory, and {raw!r} does not",
                {"id": raw, "resolved": str(target)},
            )
        return raw

    # -- uploads ----------------------------------------------------------------------------

    def _upload_dir(self) -> Path:
        """`<outdir>/uploads/`, created on first use -- the same lazy-`mkdir` shape as
        `_chat_dir`, and for the same reason: a `GET`-only session that never uploads a frame
        should not leave an empty `uploads/` behind on an outdir that may be a mounted disk.
        """
        directory = Path(self.server.outdir) / UPLOAD_DIR
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _upload_target(self, sanitized_name: str) -> Path:
        """`<outdir>/uploads/<stamp>-<sanitized_name>`, or `path_outside_root`.

        `resolve_within`, then a second, independent line confirming the result is one file
        directly inside the uploads directory -- the same belt-and-braces shape `_chat_path` and
        `_job_id_of` both use for a name that arrived from outside. `sanitized_name` has already
        been through `sanitize_upload_name` by the time it reaches here (`_upload_frame` is the
        only caller), but this check does not trust that: a whitelist upstream is not a reason to
        skip the path check downstream, exactly as `resolve_prompt_name`'s docstring says of
        `PROMPT_NAME`.
        """
        directory = self._upload_dir()
        target = resolve_within(directory / f"{_upload_stamp()}-{sanitized_name}",
                                {"uploads": directory}, write=True)
        if target.parent != directory.expanduser().resolve():
            raise CliError(
                "path_outside_root",
                f"an upload names one file in the uploads directory, and {sanitized_name!r} "
                "does not",
                {"name": sanitized_name, "resolved": str(target)},
            )
        return target

    def _upload_body(self, limit: int) -> bytes:
        """The raw bytes of an upload request, bounded by `limit` -- see `CHAT_IMAGE_MAX_BYTES`'s
        own docstring for why `MAX_BODY_BYTES` never enters this route at all. `limit` is chosen by
        the caller (`_upload_frame`) from the upload's own suffix -- `CHAT_IMAGE_MAX_BYTES` for a
        keyframe, `UPLOAD_AUDIO_MAX_BYTES` for an imported track (task 6, "Проекты").

        Read in one call by an honest `Content-Length`, the same shape `_json_request` reads its
        own body with and for the same reason: the alternative, reading until the connection
        closes, would block forever on a request that promised bytes and never sent them.
        """
        raw_length = self.headers.get("Content-Length") if self.headers else None
        try:
            length = int(raw_length or 0)
        except ValueError as exc:
            raise CliError("bad_request", f"Content-Length is not a number: {raw_length!r}",
                           {"content_length": raw_length}) from exc
        if length <= 0:
            raise CliError("bad_request", "the upload body is empty",
                           {"content_length": length})
        if length > limit:
            # Мегабайты, а не байты: «файл больше 16777216 байт (18446744)» человек читает как
            # два незнакомых числа, а условие тут одно и простое.
            raise CliError(
                "bad_image",
                f"файл больше {limit // (1024 * 1024)} МБ ({length / (1024 * 1024):.1f} МБ)",
                {"content_length": length, "limit": limit})
        return self.rfile.read(length)

    def _upload_frame(self) -> tuple[int, str, bytes]:
        """`POST /api/uploads`: one keyframe -- or, since task 6 ("Проекты"), one finished mp3 a
        person wants to import as a `kind="clip"` project's track -- from disk, dropped or picked
        in the browser, saved under `<outdir>/uploads/` and answered with the path the rest of the
        page already knows how to use -- `#image`/`#end-image` have always taken a path, never a
        file, so a drop zone only has to produce one and put it there (see `h3_48gb/webui/app.js`);
        `POST /api/projects`'s own `track_path` (task 6) takes the same shape of path.

        The body is raw bytes with `X-Filename` naming the file, not JSON -- neither an image nor
        an mp3 fits inside a JSON string without a base64 detour this route has no reason to make
        when the bytes can simply be the body.

        **Cheap checks before expensive ones.** `X-Filename` is read and sanitized, and the
        suffix it produces is checked against `CHAT_IMAGE_SUFFIXES`/`UPLOAD_AUDIO_SUFFIXES`, before
        a single byte of the body is read: a `.txt` dropped on the zone by mistake is refused off
        the headers alone, the same order `_serve_file` uses (`resolve` -> `suffix` -> read) and
        for the same reason -- a refusal should not cost the I/O of the thing being refused. The
        suffix also decides which size limit applies (`CHAT_IMAGE_MAX_BYTES` for an image,
        `UPLOAD_AUDIO_MAX_BYTES` -- larger, a song runs minutes -- for an mp3), read before the
        body for the same reason.
        """
        header_name = self._sole_header("X-Filename", "bad_request")
        if not header_name:
            raise CliError("bad_request", "X-Filename is required and must name one file",
                           {"header": "X-Filename"})
        # An HTTP header value is ISO-8859-1 by the letter of the spec and, in every browser that
        # matters here, enforced as ByteString by `fetch`/`XMLHttpRequest` itself -- a Cyrillic
        # name would throw in the page before the request was even sent. The page therefore sends
        # `encodeURIComponent(file.name)` (`app.js`), and this is the matching `decodeURIComponent`
        # on the way back in; a plain ASCII name round-trips through both unchanged, since nothing
        # in it is a percent-sign.
        raw_name = urllib.parse.unquote(header_name)
        sanitized = sanitize_upload_name(raw_name)
        suffix = Path(sanitized).suffix.lower()
        if suffix in UPLOAD_AUDIO_SUFFIXES:
            limit = UPLOAD_AUDIO_MAX_BYTES
        elif suffix in CHAT_IMAGE_SUFFIXES:
            limit = CHAT_IMAGE_MAX_BYTES
        else:
            raise CliError(
                "bad_image",
                f"файлом может быть png, jpg, webp или mp3, а {sanitized!r} — нет "
                f"(разрешение/длительность любые, размер до "
                f"{CHAT_IMAGE_MAX_BYTES // (1024 * 1024)} МБ для картинки, "
                f"{UPLOAD_AUDIO_MAX_BYTES // (1024 * 1024)} МБ для mp3)",
                {"name": sanitized, "suffix": suffix,
                 "allowed": sorted(CHAT_IMAGE_SUFFIXES | UPLOAD_AUDIO_SUFFIXES)},
            )
        data = self._upload_body(limit)
        target = self._upload_target(sanitized)
        try:
            target.write_bytes(data)
        except OSError as exc:
            raise CliError("queue_unwritable", f"файл не сохранился: {target} ({exc})",
                           {"path": str(target), "error": f"{type(exc).__name__}: {exc}"}) from exc
        return 200, "application/json", _json_bytes({"ok": True, "path": str(target)})

    # -- jobs -------------------------------------------------------------------------------

    def _submit_job(self) -> tuple[int, str, bytes]:
        """`POST /api/jobs`: validate, estimate, and put one job in `pending/`.

        `queue.submit` receives the *text* of the prompt and writes the snapshot itself -- the
        snapshot's path contains the job id, and the id does not exist until `submit` claims it.
        """
        payload = self._json_request(allowed=("args", "note"))
        # The body's own shape is checked before a subprocess is spawned for it: a `note` that is
        # a number is the caller's mistake and should not cost a fork to discover.
        args, note = self._args_of(payload), self._note_of(payload)
        prepared = prepare_submission(args, self.server.roots)
        _refuse_if_relocation_escapes_the_roots(
            self.server.queue_root, prepared["args"], prepared["report"]["output_stem"],
            self.server.roots)
        with queue_write_errors(self.server.queue_root, what="--tag"):
            job = q.submit(self.server.queue_root, prepared["args"], note,
                           prepared["report"], prepared["estimate"],
                           prompt_source=prepared["prompt_source"],
                           prompt_text=prepared["prompt_text"])
        return 200, "application/json", _json_bytes(
            {"ok": True, "job": job.as_dict(), "estimate": prepared["estimate"]})

    def _edit_job(self, raw_id: str) -> tuple[int, str, bytes]:
        """`PUT /api/jobs/<id>`: the same validation as submission, applied in place.

        The same pipeline rather than a lighter one on purpose: an edit that skipped the dry run
        would be the way to get an argument list into the queue that submission would have refused.
        """
        job_id = self._job_id_of(raw_id)
        # Task 6 ("Проекты"): a project scene's own `generate` job (`note` shaped by
        # `assemble.scene_note`) is not a free-standing job a person may hand-edit -- its `note`
        # is the only thing tying it back to `scenes[idx]`, and rewriting args/note out from under
        # that tie would desynchronise it from `scenes[idx].job_id` (task 4's C1/C2 contract; see
        # `h3_48gb.assemble`'s own module docstring). Checked against the job as it stands *before*
        # this edit -- `note` is one of the fields the edit itself might change, so the source of
        # truth here is what is on disk right now, not what the request is asking `note` to become.
        jobs, _broken = q.scan(self.server.queue_root)
        current = next((j for j in jobs if j.id == job_id and j.state == "pending"), None)
        if current is not None and assemble_module.parse_scene_note(current.note) is not None:
            raise CliError(
                "project_scene_locked",
                f"эта задача — сцена проекта, её нельзя редактировать вручную: {job_id}",
                {"id": job_id, "note": current.note})
        payload = self._json_request(allowed=("args", "note"))
        args, note = self._args_of(payload), self._note_of(payload)
        prepared = prepare_submission(args, self.server.roots)
        with queue_write_errors(self.server.queue_root, what="the job id"):
            job = q.update(self.server.queue_root, job_id, prepared["args"],
                           note, prepared["report"], prepared["estimate"],
                           prompt_source=prepared["prompt_source"],
                           prompt_text=prepared["prompt_text"])
        return 200, "application/json", _json_bytes(
            {"ok": True, "job": job.as_dict(), "estimate": prepared["estimate"]})

    def _promote_job(self, raw_id: str) -> tuple[int, str, bytes]:
        job_id = self._job_id_of(raw_id)
        with queue_write_errors(self.server.queue_root, what="the job id"):
            job = q.move_to_front(self.server.queue_root, job_id)
        return 200, "application/json", _json_bytes({"ok": True, "job": job.as_dict()})

    def _duplicate_job(self, raw_id: str) -> tuple[int, str, bytes]:
        """`POST /api/jobs/<id>/duplicate`: re-queue a `pending`, `done` or `failed` job's `args`
        and `note` as a new `pending` job. Answers `{"id": <the new job's id>}`.

        The source is looked up with `q.scan`, not with `_job_id_of` (which only ever builds a
        path inside `pending/`): the source's state is not known up front, so which directory to
        build a path into is not either, and `raw_id` is only ever compared against `Job.id` --
        it never becomes a path itself, so there is nothing here for a traversal id to reach.

        `dry_run_report`/`estimate` are **not** recomputed by re-running `prepare_submission`'s
        dry-run subprocess: they are read straight off the source job's own file (`estimate`,
        `output_stem`), the same numbers that job queued with the first time, because args that
        have not changed dry-run to the same report every time. This also means a duplicate is
        never re-validated against the command allowlist or the roots -- the source already
        passed both once, or it would not be sitting in the queue at all.

        The source's own `output_stem`, unchanged, would collide -- see
        `_duplicate_tag_candidates` -- so candidates from it are tried in order until one of
        them clears `queue.submit`'s conflict check.

        If the source used `--prompt-file`, its value by now names *the source's own* snapshot
        (`queue/prompts/<source-id>.txt` -- `submit` repoints it there the first time, and an
        edit or a duplicate that never passes `prompt_text` leaves it alone, see `queue.submit`'s
        docstring). Left as-is, the duplicate would depend on a file `queue.cancel` deletes the
        moment the source job is withdrawn -- fine right up until the worker actually claims the
        duplicate and finds `--prompt-file` unreadable, long after anyone would think to connect
        the two. So the snapshot's *content* is read here and passed through as `prompt_text`:
        `submit` then snapshots it again, to `queue/prompts/<new-id>.txt`, and repoints `args`
        itself -- the duplicate ends up with a copy of its own, independent of the source's.
        """
        jobs, _broken = q.scan(self.server.queue_root)
        job = next((candidate for candidate in jobs if candidate.id == raw_id
                   and candidate.state in ("pending", "done", "failed")), None)
        if job is None:
            return 404, "application/json", _error_bytes(
                "not_found", f"нет такой задачи: {raw_id}", {"id": raw_id})

        # I3 (fix round 1, 2026-08-18 review): a `kind="song"`/`"assemble"` job's `args` names a
        # *project* (`["song", "--project", <path>]`), not a canvas/prompt the way `generate`'s do
        # -- resubmitting it as a fresh pending job the way this route does for `generate` would
        # start a second song/assemble run racing the original against the same project, or (worse)
        # succeed only because `_duplicate_tag_candidates`' `--tag` rewrite happens to be a no-op on
        # args that were never `--tag`-shaped to begin with. What "duplicate a song/assemble job"
        # should actually mean (a new track dir? the same one?) is a decision for task 6, not a
        # guess made here -- so this refuses honestly instead. This must also be the reason `submit`
        # below is given `kind=job.kind` rather than defaulting to `KIND_GENERATE`: without either
        # the refusal here or the correct `kind`, a duplicated song job would have silently become a
        # `generate` job under `queue.submit`'s new M4 args-shape validation and failed there
        # instead, an equally honest but far more confusing refusal.
        if job.kind != q.KIND_GENERATE:
            return 409, "application/json", _error_bytes(
                "duplicate_unsupported_kind",
                f"дублирование задач kind={job.kind!r} пока не поддержано (появится в Task 6): "
                f"{raw_id}",
                {"id": raw_id, "kind": job.kind})

        # Task 6 ("Проекты"): a `kind="generate"` job can still be a project scene's own job --
        # `note` shaped by `assemble.scene_note` -- and duplicating one would produce a second job
        # nothing on the project ever points at (`scenes[idx].job_id` still names the original),
        # invisible to the scene chain and to a human retrying the scene from its project page.
        # Refused the same honest way the kind check just above refuses song/assemble.
        parsed_scene = assemble_module.parse_scene_note(job.note)
        if parsed_scene is not None:
            project_id, idx = parsed_scene
            return 409, "application/json", _error_bytes(
                "project_scene_locked",
                f"эта задача — сцена проекта {project_id} #{idx}, дублировать её нельзя "
                f"(пересчитайте сцену со страницы проекта): {raw_id}",
                {"id": raw_id, "project": project_id, "idx": idx})

        prompt_text = None
        if "--prompt-file" in job.args and job.args.index("--prompt-file") + 1 < len(job.args):
            snapshot = Path(job.args[job.args.index("--prompt-file") + 1])
            try:
                prompt_text = snapshot.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise CliError(
                    "prompt_file_unreadable",
                    f"--prompt-file could not be read: {snapshot} ({exc})",
                    {"path": str(snapshot)},
                ) from exc

        last_error: CliError | None = None
        candidates = _duplicate_tag_candidates(job.args, job.output_stem)
        for _ in range(DUPLICATE_ATTEMPTS):
            args, output_stem = next(candidates)
            _refuse_if_relocation_escapes_the_roots(
                self.server.queue_root, args, output_stem, self.server.roots)
            try:
                with queue_write_errors(self.server.queue_root, what="the job id"):
                    new_job = q.submit(self.server.queue_root, args, job.note,
                                       {"output_stem": output_stem}, dict(job.estimate),
                                       prompt_source=job.prompt_source, prompt_text=prompt_text,
                                       kind=job.kind)
            except CliError as exc:
                if exc.code != "output_stem_conflict":
                    raise
                last_error = exc
                continue
            return 200, "application/json", _json_bytes({"id": new_job.id})
        raise last_error

    def _reveal_job(self, raw_id: str) -> tuple[int, str, bytes]:
        """`POST /api/jobs/<id>/reveal`: open Finder at a `done` or `failed` job's own output.

        `pending`/`running` are refused with `not_found`, the same as an unknown id: a `pending`
        job has no output yet worth revealing, and one still in `running/` could point Finder at a
        file the worker is writing to this very second (`_promote_job`, `_cancel_job` and this
        route are the only three that ever look a job up by bare id across every state the way
        `_duplicate_job` does, and this one narrows the set on purpose).

        The target is the job's own clip if it made it to disk, its own run directory otherwise
        (task A6 gives every job its own subdirectory) -- the clip is the one file a person came
        here to look at, the directory is what is left once a failed run never produced one.
        Neither existing is the same as "nothing worth showing", not "show the outdir instead":
        the button promises *this job's* output, and a consolation prize one level up would be a
        different, unrelated job's neighbour.

        `resolve_within` bounds the target to the outdir the same way `/media` bounds its own
        reads: `output_stem` is job data read back off disk here, not re-validated against the
        roots the way a fresh submission is (`_refuse_if_relocation_escapes_the_roots`), so a job
        written before some future change to that policy is exactly the case this still guards.

        `self.server.reveal` is the seam a test replaces (`make_server(..., reveal=...)`); in
        production it is `_reveal_in_finder`, which shells out to `open -R`.
        """
        jobs, _broken = q.scan(self.server.queue_root)
        job = next((candidate for candidate in jobs if candidate.id == raw_id
                   and candidate.state in ("done", "failed")), None)
        if job is None:
            return 404, "application/json", _error_bytes(
                "not_found", f"нет такой законченной задачи: {raw_id}", {"id": raw_id})

        stem = Path(job.output_stem)
        candidates = (stem.with_name(stem.name + ".mp4"), stem.parent)
        target = next((path for path in candidates if path.exists()), None)
        if target is None:
            return 404, "application/json", _error_bytes(
                "not_found", f"на диске не осталось файлов задачи: {raw_id}", {"id": raw_id})

        resolved = resolve_within(target, {"outdir": Path(self.server.outdir)}, write=False)
        self.server.reveal(resolved)
        return 200, "application/json", _json_bytes({"ok": True, "path": str(resolved)})

    def _cancel_job(self, raw_id: str) -> tuple[int, str, bytes]:
        """`DELETE /api/jobs/<id>`: cancel a job still in `pending/`, or -- task 2's "Удалить" on
        a finished card -- remove a `done`/`failed` job's own record and its run's artifacts.

        Two different operations behind one route on purpose: `_route_delete` dispatches only on
        the URL, and the page cannot know a job's state without first asking, so the state decides
        which one runs here rather than the caller having to pick a different URL for each. A job
        in `running/` gets neither -- the worker is mid-generation, and stopping it is a different,
        harder feature nobody asked for here -- so it answers exactly what this route always
        answered before this button existed: `job_not_pending`.

        `pending/<id>.json` existing right now is only the routing decision; the real authority
        for the cancel branch is still `q.cancel`'s own lock (a job claimed between this check and
        that call raises `JobNotPending` there, same as it always could).

        `job_id` -- not `raw_id` -- is what the second branch matches against `q.scan`'s own
        `Job.id`: `_job_id_of` already proved `raw_id` is a bare id with no `/` or `..` in it (the
        same check `job_path(..., "pending")` needs to be safe), and that proof holds for
        `done/<id>.json` and `failed/<id>.json` exactly as it does for `pending/<id>.json` --
        `job_id` has no directory components at all, in any of the three. Matching by identity
        (`==`) rather than trusting `q.scan`'s own `Job.id` blind matters here in a way it does
        not for `_reveal_job`: that route only ever compares `Job.id` to a string, but this one
        goes on to build filesystem paths from it (the record file, the prompt snapshot), and
        `Job.id` is read straight out of the job's own JSON *content* by `queue._job_from_file`,
        not derived from the filename that scan found it under.
        """
        job_id = self._job_id_of(raw_id)
        # `name_too_long_is_a_refusal` around the routing check too, not only around `q.cancel`:
        # `.exists()` on a 400-character `pending/<id>.json` raises the same `ENAMETOOLONG` this
        # context manager already turns into a 400 for `q.cancel` -- without it here, that OSError
        # reached the handler's `internal_error` net one line before the check that used to catch
        # it, and a caller-controlled length answered 500 again.
        with name_too_long_is_a_refusal("the job id"):
            pending = q.job_path(self.server.queue_root, job_id, "pending").exists()
        if pending:
            with queue_write_errors(self.server.queue_root, what="the job id"):
                job = q.cancel(self.server.queue_root, job_id)
            return 200, "application/json", _json_bytes({"ok": True, "job": job.as_dict()})

        jobs, _broken = q.scan(self.server.queue_root)
        job = next((candidate for candidate in jobs if candidate.id == job_id
                   and candidate.state in ("done", "failed")), None)
        if job is None:
            raise CliError(
                "job_not_pending",
                f"эта задача не ждёт в очереди и ещё не завершилась (возможно, уже идёт): {job_id}",
                {"id": job_id},
            )
        return self._delete_finished_job(job)

    def _delete_finished_job(self, job) -> tuple[int, str, bytes]:
        """The second half of `_cancel_job`: `job` is already `done` or `failed`. Removes its run's
        own artifacts from disk, then its record from the queue -- in that order, so a failure
        partway through artifact cleanup (a permission error, a half-mounted disk) leaves the
        record in `done/`/`failed/` rather than a job whose files silently outlived the row that
        named them; the click is then retryable instead of having quietly done half its job.

        Two shapes of run, told apart by `_is_relocated_job_subdir`:

        * **Relocated** (task A6, every job queued since): `output_stem`'s own directory is that
          job's, and nothing else's -- `shutil.rmtree` is correct and complete. Already gone
          (a person cleaned it up by hand, or clicked delete twice) is not an error here: the
          desired end state, "the directory does not exist", already holds.
        * **Flat** (queued before A6, or a `--outdir` a person pointed at a shared or date-only
          folder by hand): the directory can hold other runs' files, or a person's own, so only
          the files `output_stem` itself names are removed (`_delete_flat_artifacts`) -- the
          directory itself is never touched.

        `resolve_within` bounds `run_dir` to the outdir the same way `/media` and `_reveal_job`
        bound their own reads: `output_stem` is job data read back off disk, not re-validated
        against the roots the way a fresh submission is.
        """
        outdir = Path(self.server.outdir).resolve()
        stem = Path(job.output_stem)
        run_dir = stem.parent
        resolved_run_dir = resolve_within(run_dir, {"outdir": outdir}, write=True)
        if _is_relocated_job_subdir(run_dir, resolved_run_dir, outdir):
            if resolved_run_dir.exists():
                shutil.rmtree(resolved_run_dir)
        else:
            _delete_flat_artifacts(stem, job)

        q.job_path(self.server.queue_root, job.id, job.state).unlink(missing_ok=True)
        q.prompt_path(self.server.queue_root, job.id).unlink(missing_ok=True)
        return 200, "application/json", _json_bytes({"ok": True, "id": job.id})

    def _estimate_only(self) -> tuple[int, str, bytes]:
        """`POST /api/estimate`: the cost of an argument list, without queueing anything.

        Normally no subprocess: the estimate is a formula, and the form recomputes it on every
        keystroke. The command allowlist and the path check still run -- without the path check
        this route would read `quant_config.json` from any directory a caller named, which turns
        an estimate into an existence oracle for the whole filesystem.

        **The one exception is a keyframe run with no canvas** (`canvas_comes_from_the_image`),
        which the form now sends deliberately: «из кадра (авто)» omits `--width`/`--height` so the
        CLI derives the canvas from the frame, aspect intact. The formula cannot follow it there --
        the derivation lives in `minimax_h3_mlx.packing`, which this process may never import (see
        the module docstring and `test_web_module_does_not_import_mlx`) -- so without the dry run
        `estimate` would silently fall back to `DEFAULT_CANVAS`, i.e. price a vertical frame as a
        landscape video. Duplicating the arithmetic here instead would be the worse answer: two
        implementations of the same rule drift, and this one would drift towards *quietly wrong
        numbers* rather than an error.

        The subprocess is affordable exactly because it is bounded to this case: a dry run with a
        keyframe measures ~0.12 s (it opens the image and builds a `RunSpec`, no weights), the form
        debounces estimates by 250 ms, and every other keystroke -- a preset, a duration, a step
        count -- still takes the formula-only path.
        """
        payload = self._json_request(allowed=("args",))
        args = self._args_of(payload)
        _check_command_allowed(_parse_args(args))
        # The *normalised* list, for the same reason submission uses it: `--checkpoint
        # ~/models/h3-8bit` reaches `quant_bits` as a directory literally named `~` otherwise, and
        # the form would be told 4 bits here and 8 bits on submission for one request.
        argv = check_path_flags(args, self.server.roots)
        parsed = _parse_args(argv)
        report = validate_args(_argv_for_canvas_dry_run(argv, parsed)) \
            if canvas_comes_from_the_image(parsed) else None
        return 200, "application/json", _json_bytes(
            {"ok": True, "estimate": estimate(argv, checkpoint=parsed.checkpoint, report=report)})

    # -- projects (task 6, "Проекты") --------------------------------------------------------

    def _project_dir(self, raw_id: str) -> Path:
        """`<outdir>/projects/<raw_id>/`, or `path_outside_root` -- the same "id is a path
        component, not a path" discipline `_job_id_of`/`_chat_path` already apply: `raw_id` comes
        out of a URL and becomes a directory name, so `../../etc` has to be refused before
        anything touches the filesystem with it.
        """
        projects_root = Path(self.server.outdir) / "projects"
        target = resolve_within(projects_root / raw_id, {"outdir": Path(self.server.outdir)},
                                write=True)
        if target.parent != projects_root.expanduser().resolve():
            raise CliError(
                "path_outside_root",
                f"a project id names one directory under projects/, and {raw_id!r} does not",
                {"id": raw_id, "resolved": str(target)},
            )
        return target

    def _load_project(self, raw_id: str):
        """The `project.Project` `raw_id` names, or `project_not_found` -- every project route
        below starts here.
        """
        directory = self._project_dir(raw_id)
        try:
            return project_module.load_project(directory)
        except project_module.ProjectNotFound as exc:
            raise CliError("project_not_found", f"нет проекта {raw_id}: {exc}",
                           {"id": raw_id}) from exc

    def _list_projects(self) -> tuple[int, str, bytes]:
        """`GET /api/projects`: every project under `<outdir>/projects/`, summarised -- design
        spec, "Веб": "список: название, kind, этап, прогресс сцен".

        I2 (fix round 1, 2026-08-19 review): the queue is scanned once, here, and the same `jobs`
        list is handed to every `project_summary` call -- not one `q.scan` per project, which is
        what this route (and `build_state`'s own "projects" key) used to do.
        """
        projects = project_module.list_projects(self.server.outdir)
        with queue_errors(self.server.queue_root):
            jobs, _broken = q.scan(self.server.queue_root)
        return 200, "application/json", _json_bytes(
            {"ok": True, "projects": [project_summary(proj, jobs) for proj in projects]})

    def _read_project(self, raw_id: str) -> tuple[int, str, bytes]:
        """`GET /api/projects/<id>`: the full `project.json`, plus the one queue job (if any) that
        explains what it is doing right now (`_project_active_job` -- Task 3's M6: the project
        file alone cannot say "a song/assemble job is running").
        """
        proj = self._load_project(raw_id)
        with queue_errors(self.server.queue_root):
            jobs, _broken = q.scan(self.server.queue_root)
        return 200, "application/json", _json_bytes(
            {"ok": True, "project": proj.as_dict(), "active_job": _project_active_job(proj, jobs)})

    def _create_project(self) -> tuple[int, str, bytes]:
        """`POST /api/projects`: a new project, either from a chat session's last `project` field
        (task 5's `session["project"]` -- `{kind, scenes, lyrics, caption}`, task 5 report) or
        empty, for a `kind` given directly (design spec: "POST /api/projects (kind + сцены/лирика
        из последнего project-поля сессии, или пустого)").

        **`session["project"]` is not trusted just because it is there** (task 5 report, "сомнение
        3": stored without validating its shape against `PROMPT_SCHEMA`) -- every field this route
        reads out of it is checked here, honestly, with `args_invalid` naming exactly what is
        wrong, never a 500: an external provider outside `response_format`'s reach can put anything
        in that field, and this is the boundary where garbage from the model becomes a project on
        disk or a clear refusal, not an unhandled exception.

        `kind` from the request body, if given, overrides the session's own -- a "Новый проект"
        button (task 7) that already knows what it wants does not have to fabricate a chat session
        first. Without either, `args_invalid`.

        `track_source: "import"` (design spec addendum, "импортированный трек") is only valid for
        `kind="clip"`: `track_path` must already be an uploaded file (`POST /api/uploads`, extended
        by this same task to accept `.mp3`) inside one of this server's own roots and end in
        `.mp3` -- checked with `resolve_within`, the same policy every other path this server
        accepts goes through.

        Populates `scenes`/`track` directly (`project.save()`, a blind bulk write) rather than
        through the locked mutators -- `project.py`'s own docstring names this exact moment ("the
        script gate ... populating a freshly created project's scenes/track for the first time")
        as `save()`'s intended use: nothing else has touched this project yet, so there is nothing
        for a blind write to clobber. `stages.script` becomes `"awaiting_approval"` only when there
        is actually something to approve (a video project got at least one scene, or a clip/song
        project got non-empty lyrics) -- an empty project (no session, or a session with no
        `project` field) stays `"draft"`, not yet approvable, exactly as the design spec's "или
        пустого" describes: a shell a person fills in later, not an empty gate.
        """
        payload = self._json_request(
            allowed=("session_id", "kind", "title", "track_source", "track_path"))

        session_id = self._string_of(payload, "session_id")
        session = None
        session_project = None
        if session_id:
            _, session = self._read_session(session_id)
            if session is None:
                raise CliError("chat_not_found", f"нет сессии {session_id}", {"id": session_id})
            raw_project = session.get("project")
            if isinstance(raw_project, dict):
                session_project = raw_project

        kind = self._string_of(payload, "kind") \
            or (session_project.get("kind") if session_project else "")
        if kind not in PROJECT_KINDS:
            raise CliError(
                "args_invalid",
                f"`kind` must be one of {sorted(PROJECT_KINDS)} (from the request body, or from "
                f"the session's own `project.kind`), and {kind!r} is not",
                {"kind": kind, "kinds": sorted(PROJECT_KINDS)})

        scenes: list[dict] = []
        if kind == "video" and session_project:
            raw_scenes = session_project.get("scenes")
            if raw_scenes is not None:
                if not isinstance(raw_scenes, list):
                    raise CliError(
                        "args_invalid", "session `project.scenes` must be a list or null",
                        {"type": type(raw_scenes).__name__})
                for i, raw in enumerate(raw_scenes):
                    if not isinstance(raw, dict):
                        raise CliError(
                            "args_invalid", f"session `project.scenes[{i}]` must be an object",
                            {"index": i, "type": type(raw).__name__})
                    prompt = raw.get("prompt")
                    duration = raw.get("duration")
                    if not isinstance(prompt, str) or not prompt.strip():
                        raise CliError(
                            "args_invalid",
                            f"session `project.scenes[{i}].prompt` must be a non-empty string",
                            {"index": i, "type": type(prompt).__name__})
                    if (not isinstance(duration, (int, float)) or isinstance(duration, bool)
                            or duration <= 0):
                        raise CliError(
                            "args_invalid",
                            f"session `project.scenes[{i}].duration` must be a positive number",
                            {"index": i, "type": type(duration).__name__})
                    scenes.append({"idx": i, "prompt": prompt, "duration": float(duration),
                                   "status": "pending", "job_id": None, "clip_path": None,
                                   "keyframe_path": None})

        lyrics = None
        caption = None
        if kind in ("clip", "song") and session_project:
            raw_lyrics = session_project.get("lyrics")
            raw_caption = session_project.get("caption")
            if raw_lyrics is not None and not isinstance(raw_lyrics, str):
                raise CliError("args_invalid", "session `project.lyrics` must be a string or null",
                               {"type": type(raw_lyrics).__name__})
            if raw_caption is not None and not isinstance(raw_caption, str):
                raise CliError("args_invalid",
                               "session `project.caption` must be a string or null",
                               {"type": type(raw_caption).__name__})
            lyrics, caption = raw_lyrics, raw_caption

        track_source = self._string_of(payload, "track_source") or "generate"
        if track_source not in ("generate", "import"):
            raise CliError("args_invalid",
                           f"`track_source` must be 'generate' or 'import', and {track_source!r} "
                           "is not", {"track_source": track_source})
        if track_source == "import" and kind != "clip":
            raise CliError(
                "args_invalid",
                f"`track_source: import` is only for kind='clip' projects, and this one is "
                f"kind={kind!r}", {"kind": kind})
        track_path = None
        if track_source == "import":
            raw_track_path = self._string_of(payload, "track_path")
            if not raw_track_path:
                raise CliError("args_invalid",
                               "`track_path` is required when track_source='import'", {})
            # M6 (fix round 1, 2026-08-19 review): scoped to `outdir` alone, not the server's
            # whole `roots` (which also includes `repo`/`models`) -- an imported track can only
            # ever be a file this same server already accepted through `/api/uploads`
            # (`<outdir>/uploads/...`), same narrowing `resolve_prompt_name`/`_project_dir` already
            # use for their own single-root path checks.
            resolved = resolve_within(
                raw_track_path, {"outdir": Path(self.server.outdir)}, write=False)
            if resolved.suffix.lower() not in UPLOAD_AUDIO_SUFFIXES:
                raise CliError(
                    "args_invalid",
                    f"`track_path` must be an uploaded .mp3 file, and {raw_track_path!r} is not",
                    {"path": raw_track_path, "suffix": resolved.suffix})
            if not resolved.is_file():
                raise CliError("args_invalid", f"`track_path` does not exist: {raw_track_path}",
                               {"path": raw_track_path})
            track_path = resolved

        title = (self._string_of(payload, "title").strip()
                or (session.get("slug") if session else "")
                or f"{kind} project")

        proj = project_module.create_project(self.server.outdir, kind, title)
        if kind == "video":
            proj.scenes = scenes
            if scenes:
                proj.stages["script"] = "awaiting_approval"
        else:
            if lyrics is not None:
                proj.track["lyrics"] = lyrics
            if caption is not None:
                proj.track["caption"] = caption
            if track_source == "import":
                proj.track["source"] = "import"
                proj.track["mp3"] = str(track_path)
            if proj.track.get("lyrics"):
                proj.stages["script"] = "awaiting_approval"
        proj.save()

        return 200, "application/json", _json_bytes(
            {"ok": True, "id": proj.id, "project": proj.as_dict()})

    def _submit_project_song_job(self, proj) -> dict:
        """The `kind="song"` job a script-stage approval submits for a `kind="clip"`/`"song"`
        project (design spec: "сценарий approved -> (clip/song) submit song-задачи"). Same shape
        for `track.source in ("generate", "import")` -- the worker's own dispatch
        (`h3_48gb.worker._run_song_job`) is what tells the two apart, not the submission here (task
        3 report: "Дальше конвейер не различает источники").

        `output_stem` (Task 3's own recommendation, its report's "Контракты для Task 4/Task 6"):
        `<project>/track/job-song` -- under the same `track_dir` the worker writes `song.*`/
        `whisper.json` into, but not equal to any suffix `queue._stem_taken`'s `ARTIFACT_SUFFIXES`
        check looks for next to it, so a project's own song artifacts never collide with the
        conflict check meant to catch a second pending song job for the same project.
        """
        lyrics = proj.track.get("lyrics") or ""
        track_dir = proj.path.parent / "track"
        output_stem = str(track_dir / "job-song")
        args = ["song", "--project", str(proj.path)]
        note = f"project track {proj.id}"
        # `song_job_wallclock_estimate_seconds` prices a *generated* take (Music3's own ~13x
        # realtime) -- an imported track's own job only runs the much faster Whisper alignment
        # pass (`songrun.align_track`), so this number over-estimates an import's own wall clock.
        # Not fixed in this task: no validated formula for align-only timing exists yet, and the
        # estimate is display-only (`queue.Job.estimate`'s own "whatever the caller put there"
        # contract) -- see the task report.
        estimate = {"seconds": song_job_wallclock_estimate_seconds(lyrics)}
        with queue_write_errors(self.server.queue_root, what="the project track"):
            job = q.submit(self.server.queue_root, args, note, {"output_stem": output_stem},
                           estimate, kind=q.KIND_SONG)
        return {"job_id": job.id}

    def _approve_project_stage(self, raw_id: str, stage: str) -> tuple[int, str, bytes]:
        """`POST /api/projects/<id>/approve/<stage>`: the human gate (design spec, "Этапы и
        гейты") -- only `script` and `track` are gated at all (`scenes`/`assembly` are automatic,
        design spec: "дальше автомат").

        **Gates do not skip.** A stage is only approvable from `"awaiting_approval"` -- for
        `script`, that means `POST /api/projects` actually populated it (a truly empty project
        stays `"draft"` forever, unapprovable); for `track`, that means a `kind="song"` job has
        already finished (`h3_48gb.worker._run_song_job`, which is the only thing that ever moves
        `stages.track` there). Approving `track` before `script` therefore answers
        `project_stage_not_ready` (409), not because this route checks the *order* of stages, but
        because `track` genuinely never reaches `"awaiting_approval"` until a song job -- itself
        only submitted once `script` is approved -- has actually run.

        **`proj.approve_stage(stage)` runs *last*, only once every side effect below has actually
        succeeded (C1, fix round 1, 2026-08-19 review).** The previous order -- `approve_stage`
        first, side effect after -- committed `"approved"` to `project.json` before
        `build_clip_scenes`/`_submit_project_song_job` had a chance to fail; a failure left the
        stage stuck `"approved"` forever with nothing actually submitted or built: a repeat
        `POST .../approve/<stage>` answers `409 project_stage_not_ready` (the stage is not
        `"awaiting_approval"` any more), and neither `scenes/<idx>/retry` nor `assembly/retry`
        exist to fix a stage that never produced anything to retry in the first place -- the
        project was dead with no way back in short of hand-editing `project.json`. Doing the side
        effect first and the approval last needs no rollback on failure: nothing here has to check
        whether `advance_project` (video's `script` gate) needs `stages.script` already
        `"approved"` to do its job, because it does not -- `advance_project` re-derives everything
        it needs from `proj.scenes`/`proj.stages.scenes`, never from `stages.script`/`stages.track`
        (see its own docstring's "three outcomes" -- none of them reads either). If a side effect
        *does* raise, the stage is simply left exactly where it was (`"awaiting_approval"`), and the
        same `POST .../approve/<stage>` call is the retry -- no separate recovery endpoint needed.

        **What each gate starts, once passed:**

        * `script`, `kind="video"` -- `assemble.advance_project` (the same call `Task 4`'s own
          contract requires: "approve сценария video-проекта = advance_project, он сам сабмитит
          сцену 0" -- scene 0's own submission is *not* reimplemented here).
        * `script`, `kind in ("clip", "song")` -- a `kind="song"` job (`_submit_project_song_job`).
        * `track`, `kind="clip"` -- `build_clip_scenes` turns the now-measured track into a
          coverage-complete scene list (`proj.scenes`, `proj.save()` -- nothing has written scenes
          for this project before this moment, so a blind bulk write is safe, same reasoning as
          `_create_project`'s own), then `assemble.advance_project` submits scene 0, exactly like a
          video project's script gate does.
        * `track`, `kind="song"` -- nothing to submit: the mp3 already is the product (design spec:
          "для kind=song проект на этом завершён"). **M2 (fix round 1, 2026-08-19 review):**
          `stages.scenes`/`stages.assembly` are explicitly set to `"done"` here rather than left at
          whatever they were created with (`"draft"`, forever) -- a `kind="song"` project has no
          scenes and no assembly step at all, and leaving those two stages at `"draft"` reads, on
          the project list (`scenes_total: 0`, `stages.assembly: "draft"`), as "not started yet"
          rather than "this project is finished" -- indistinguishable from a project nobody has
          touched. `"done"` is the terminal `STAGE_STATUSES` value every other stage's own pipeline
          already lands on for "nothing more to do here"; documented here for Task 7's own list/
          detail rendering: a `kind="song"` project is complete exactly when `stages.track ==
          "approved"` (equivalently, once `stages.scenes`/`stages.assembly` read `"done"`).
        * `track`, `kind="video"` -- **M3 (fix round 1, 2026-08-19 review): refused explicitly**
          (`409 project_stage_not_ready`), not silently accepted. An optional song on a video
          project (design spec, "Суть": "звук ... опционально песня") is out of this task's v1
          scope: nothing here ever moves a video project's `stages.track` to `"awaiting_approval"`
          in the first place (script approval for `kind="video"` never submits a song job), so this
          combination is unreachable through the ordinary routes -- but the gate check above only
          looks at `stages.track`'s own value, not `proj.kind`, so a project coaxed into this shape
          by hand (or by a future bug) used to fall through every `if`/`elif` below with no side
          effect *and* no refusal, silently answering `ok: true` having approved a stage that meant
          nothing. Refusing it by name is honest about "this is not supported", not "it worked".
        """
        if stage not in ("script", "track"):
            raise CliError(
                "args_invalid", f"`stage` must be 'script' or 'track', and {stage!r} is not",
                {"stage": stage})
        proj = self._load_project(raw_id)
        current = proj.stages.get(stage)
        if current != "awaiting_approval":
            raise CliError(
                "project_stage_not_ready",
                f"стадия {stage!r} проекта {raw_id} не ждёт утверждения (сейчас {current!r})",
                {"id": raw_id, "stage": stage, "status": current})

        result: dict = {"stage": stage}
        if stage == "script":
            if proj.kind == "video":
                result["advance"] = assemble_module.advance_project(
                    proj, self.server.queue_root, self.server.outdir)
            elif proj.kind in ("clip", "song"):
                result["submit"] = self._submit_project_song_job(proj)
        elif stage == "track" and proj.kind == "clip":
            try:
                built = build_clip_scenes(proj.track)
            except ProjectSceneBuildError as exc:
                raise CliError("project_scene_build_failed", str(exc), {"id": raw_id}) from exc
            proj.scenes = built
            proj.save()
            result["advance"] = assemble_module.advance_project(
                proj, self.server.queue_root, self.server.outdir)
        elif stage == "track" and proj.kind == "song":
            proj.set_stage_status("scenes", "done")
            proj.set_stage_status("assembly", "done")
        elif stage == "track" and proj.kind == "video":
            raise CliError(
                "project_stage_not_ready",
                f"проект {raw_id}: 'track' не гейтуется для kind='video' (опциональная песня "
                f"вне v1)", {"id": raw_id, "stage": stage, "kind": proj.kind})

        proj.approve_stage(stage)

        reloaded = project_module.load_project(proj.path)
        return 200, "application/json", _json_bytes(
            {"ok": True, **result, "project": reloaded.as_dict()})

    def _retry_project_track(self, raw_id: str) -> tuple[int, str, bytes]:
        """`POST /api/projects/<id>/track/retry`: task 7's own small addition to the server ("
        мелочь можно, с тестом" -- task 6 shipped no route for this) -- "Пересчитать трек" (design
        spec, "Трек": "Гейт: прослушать, «Утвердить трек» (или пересчитать с другими seed/
        caption)"). Resubmits the same `kind="song"` job `_approve_project_stage`'s own `script`
        gate submits, before the track has been approved.

        **`kind` must be `"clip"` or `"song"`.** A `kind="video"` project's `stages.track` sits at
        `"draft"` forever too (M3, task 6 fix round 1) -- without this check it would read exactly
        like a `clip`/`song` project that has not started its track yet, and this route would
        submit a song job for a project that never asked for one.

        **`script` must already be `"approved"`.** `stages.track` starts at `"draft"` for every
        fresh project, including one whose script gate nobody has passed -- `_approve_project_
        stage`'s own script gate never has to check this explicitly (`track` cannot reach
        `"awaiting_approval"` before a song job that only `script`'s approval submits), but this
        route can be reached at `"draft"` before that job ever ran, so it checks what the approve
        route gets for free.

        **Allowed only from `"draft"` (never started, or a previous attempt's job never even
        reached the worker) or `"awaiting_approval"` (a finished take nobody has approved yet, and
        a different seed is wanted)** -- `"approved"`/`"done"` are refused: a clip's own scenes may
        already be built on top of that take (`build_clip_scenes`), and redoing the track out from
        under them is not this route's job. Not reachable at all in practice, but refused the same
        way rather than left to `_submit_project_song_job`'s own `output_stem_conflict`, is
        `"running"` -- see the next check.

        **Refused with `project_running` while a song job for this project is already pending or
        running** -- `stages.track` alone cannot say that (task 6's own M6: it stays `"draft"`
        while the very first song job is still queued), so this reuses `project_is_active`, the
        same queue join `DELETE /api/projects/<id>` already trusts for the identical question.

        **`seed`, optional.** Applied via `proj.update_track(seed=...)` *before* the resubmit, so
        the worker's own `track.get("seed")` read (`worker._run_song_job`) picks it up -- the same
        mechanism a `"Копия"` on a `done` song job already relies on to reproduce a take (task 6
        report, I1). Refused for `track.source == "import"`: an imported track has no generation
        step to seed, only the same Whisper alignment pass every retry of it would repeat
        identically.

        **`stages.track` is moved to `"running"` only after `_submit_project_song_job` actually
        succeeds, not before** -- flipping it first and having the submit itself fail would strand
        the stage at `"running"` with nothing running, the exact failure mode C1 (task 6 fix round
        1) already fixed once for `approve_stage` itself. Moving it at all (rather than leaving it
        at `"awaiting_approval"`) is what stops a stale approve of the *previous* take from landing
        while the new one is still in flight: `_approve_project_stage`'s own gate only ever accepts
        `"awaiting_approval"`. The worker's own completion (`_run_song_job`) sets it back to
        `"awaiting_approval"` unconditionally once the new take is ready, exactly as it already
        does for a project's very first attempt. The gap between a successful submit and this
        write is the same kind of narrowed-not-closed race `_cancel_project_scene_tail_jobs`'s own
        docstring already accepts as best effort by construction.
        """
        proj = self._load_project(raw_id)
        if proj.kind not in ("clip", "song"):
            raise CliError(
                "project_stage_not_ready",
                f"проект {raw_id}: 'track' не пересчитывается для kind={proj.kind!r}",
                {"id": raw_id, "kind": proj.kind})
        if proj.stages.get("script") != "approved":
            raise CliError(
                "project_stage_not_ready",
                f"проект {raw_id}: сценарий ещё не утверждён, пересчитывать трек рано",
                {"id": raw_id, "stage": "script", "status": proj.stages.get("script")})
        current = proj.stages.get("track")
        if current not in ("draft", "awaiting_approval"):
            raise CliError(
                "project_stage_not_ready",
                f"трек проекта {raw_id} нельзя пересчитать сейчас (статус {current!r})",
                {"id": raw_id, "stage": "track", "status": current})
        if project_is_active(proj, self.server.queue_root):
            raise CliError("project_running", f"проект {raw_id} ещё выполняется", {"id": raw_id})

        payload = self._json_request(allowed=("seed",))
        if "seed" in payload:
            seed = payload["seed"]
            if not isinstance(seed, (int, float)) or isinstance(seed, bool):
                raise CliError("args_invalid", "`seed` must be a number",
                               {"type": type(seed).__name__})
            if proj.track.get("source") == "import":
                raise CliError(
                    "args_invalid",
                    "`seed` has no effect on an imported track (track.source == 'import')", {})
            proj.update_track(seed=int(seed))

        result = {"submit": self._submit_project_song_job(proj)}
        proj.set_stage_status("track", "running")

        reloaded = project_module.load_project(proj.path)
        return 200, "application/json", _json_bytes(
            {"ok": True, **result, "project": reloaded.as_dict()})

    def _cancel_project_scene_tail_jobs(self, proj, idx: int) -> list[str]:
        """Cancel every still-*pending* queue job that is a project-scene job for `proj`, index
        `idx` or later -- I1 (fix round 1, 2026-08-19 review), called by `_retry_project_scene`
        right before it invalidates the chain.

        `invalidate_scene_chain` only ever touches `project.json` -- it has no idea the queue even
        exists, so a scene job already sitting in `pending/` for a scene about to be invalidated
        would otherwise survive completely untouched: the worker could claim it *before* the fresh
        resubmit `advance_project` makes right after, burning GPU minutes on a clip built from a
        keyframe that is about to be replaced (or, for `idx` itself, on the exact attempt being
        retried away). Until the worker actually claims it, that orphaned job is also invisible to
        `_project_active_job`'s own first check (its scene's `job_id` was just cleared), which is
        the DELETE gate's own protection -- see that function's own I1 fix for the other half of
        this.

        Found by `assemble.parse_scene_note(job.note)`, not by any scene's own `job_id` on `proj`
        (about to be cleared by the invalidation this precedes) -- the same source `_project_active_
        job`'s own fallback reads. `q.cancel` only ever removes a job still in `pending/`; a job the
        worker already claimed (now `running/`) cannot be cancelled here or anywhere else in this
        server, and is left alone deliberately -- racing an already-running job further is not this
        route's job, and `q.cancel` would just raise `JobNotPending` for it, caught and ignored
        below like any other "already claimed by the time we got here" race.
        """
        with queue_errors(self.server.queue_root):
            jobs, _broken = q.scan(self.server.queue_root)
        cancelled = []
        for job in jobs:
            if job.state != "pending":
                continue
            parsed = assemble_module.parse_scene_note(job.note)
            if parsed is None or parsed[0] != proj.id or parsed[1] < idx:
                continue
            try:
                q.cancel(self.server.queue_root, job.id)
            except q.JobNotPending:
                # Claimed between the scan above and this call -- nothing left to cancel.
                continue
            cancelled.append(job.id)
        return cancelled

    def _retry_project_scene(self, raw_id: str, raw_idx: str) -> tuple[int, str, bytes]:
        """`POST /api/projects/<id>/scenes/<idx>/retry`: "пересчёт отдельной сцены" (design spec,
        "Клипы") -- invalidates scene `idx` and every scene after it
        (`Project.invalidate_scene_chain`, "честное предупреждение": every later scene's automatic
        keyframe was derived, transitively, from this one's own clip), then resubmits from there
        (`assemble.advance_project`, the same call the script/track gates use to start the chain).

        **Cancels the tail's own pending queue jobs first (I1, fix round 1, 2026-08-19 review),
        before invalidating anything on `project.json`** -- see `_cancel_project_scene_tail_jobs`'s
        own docstring for why this must happen at all. Cancelling first, invalidating second (rather
        than the reverse) narrows the race: `invalidate_scene_chain` clears a scene's own `job_id`
        immediately, so doing it *before* the cancellation scan would make an orphaned job
        unreachable, by `job_id`, one write earlier than necessary. Not airtight either way -- a job
        can still be claimed in the gap between this method's own scan and its `q.cancel` call, best
        effort by construction (see that method's own docstring) -- but this ordering is strictly
        better than the reverse, at zero extra cost.
        """
        try:
            idx = int(raw_idx)
        except ValueError:
            raise CliError(
                "args_invalid", f"a scene index must be an integer, and {raw_idx!r} is not",
                {"idx": raw_idx})
        proj = self._load_project(raw_id)
        self._cancel_project_scene_tail_jobs(proj, idx)
        try:
            proj.invalidate_scene_chain(idx)
        except project_module.UnknownScene as exc:
            raise CliError(
                "project_scene_not_found", f"нет сцены {idx} в проекте {raw_id}: {exc}",
                {"id": raw_id, "idx": idx}) from exc
        advance = assemble_module.advance_project(proj, self.server.queue_root, self.server.outdir)
        reloaded = project_module.load_project(proj.path)
        return 200, "application/json", _json_bytes(
            {"ok": True, "advance": advance, "project": reloaded.as_dict()})

    def _retry_project_assembly(self, raw_id: str) -> tuple[int, str, bytes]:
        """`POST /api/projects/<id>/assembly/retry`: Task 4's own contract ("Требования к Task 6"):
        a failed assembly is not retried automatically (`assemble._submit_assembly` only ever fires
        from `"draft"`) -- this is the button that resets `stages.assembly` back to `"draft"`
        (scenes untouched, they are already `"done"`) and calls `advance_project` again, which then
        sees every scene done and resubmits.
        """
        proj = self._load_project(raw_id)
        current = proj.stages.get("assembly")
        if current != "failed":
            raise CliError(
                "project_stage_not_ready",
                f"сборка проекта {raw_id} не в статусе failed (сейчас {current!r})",
                {"id": raw_id, "status": current})
        proj.set_stage_status("assembly", "draft")
        advance = assemble_module.advance_project(proj, self.server.queue_root, self.server.outdir)
        reloaded = project_module.load_project(proj.path)
        return 200, "application/json", _json_bytes(
            {"ok": True, "advance": advance, "project": reloaded.as_dict()})

    def _delete_project(self, raw_id: str) -> tuple[int, str, bytes]:
        """`DELETE /api/projects/<id>`: only a project with nothing in flight (design spec: "DELETE
        только не-running") -- `project_is_active` joins against the queue the same way
        `project_summary`'s own `active_job` does, since a project's own `stages` cannot be trusted
        alone (Task 3's M6).
        """
        directory = self._project_dir(raw_id)
        proj = self._load_project(raw_id)
        if project_is_active(proj, self.server.queue_root):
            raise CliError("project_running", f"проект {raw_id} ещё выполняется", {"id": raw_id})
        try:
            shutil.rmtree(directory)
        except OSError as exc:
            raise CliError("queue_unwritable", f"проект не удалился: {directory} ({exc})",
                           {"path": str(directory), "error": f"{type(exc).__name__}: {exc}"}) \
                from exc
        return 200, "application/json", _json_bytes({"ok": True})

    # -- prompts ----------------------------------------------------------------------------

    def _prompts_dir(self) -> Path:
        return Path(self.server.roots["repo"]) / PROMPTS_DIR

    def _list_prompts(self) -> tuple[int, str, bytes]:
        """`GET /api/prompts`: the names and sizes of `prompts/*.txt`, in name order.

        Only files whose names the page could ask for again: `resolve_prompt_name` is the rule for
        reading one, and a listing that offered `../secret.txt` or `notes.md` would be offering
        entries every other route refuses.
        """
        directory = self._prompts_dir()
        rows = []
        for candidate in sorted(directory.glob("*.txt")):
            if not PROMPT_NAME.fullmatch(candidate.name):
                continue
            try:
                if not candidate.is_file():
                    continue
                rows.append({"name": candidate.name, "bytes": candidate.stat().st_size})
            except OSError:
                continue
        return 200, "application/json", _json_bytes(
            {"ok": True, "prompts": rows, "dir": str(directory)})

    def _read_prompt(self, name: str) -> tuple[int, str, bytes]:
        path = resolve_prompt_name(name, self.server.roots["repo"])
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return 404, "application/json", _error_bytes(
                "not_found", f"нет такого промпта: {name}", {"name": name})
        except (OSError, UnicodeDecodeError) as exc:
            raise CliError("prompt_file_unreadable", f"промпт не читается: {path} ({exc})",
                           {"name": name}) from exc
        return 200, "application/json", _json_bytes(
            {"ok": True, "name": name, "text": text, "path": str(path)})

    def _save_prompt(self, name: str) -> tuple[int, str, bytes]:
        """`PUT /api/prompts/<name>`: write the text to `prompts/<name>` durably.

        Durably because everything this server confirms is durable (design spec, "Глобальные
        ограничения"): the same temp-file/fsync/replace protocol the queue uses, reused rather
        than reimplemented. Nothing is committed to git -- a silent commit out of a browser is the
        worst possible way to learn your history changed.
        """
        path = resolve_prompt_name(name, self.server.roots["repo"])
        payload = self._json_request(allowed=("text",))
        text = payload.get("text")
        if not isinstance(text, str):
            raise CliError("args_invalid", "`text` must be a string",
                           {"type": type(text).__name__})
        try:
            q.write_text_durably(path, text)
        except OSError as exc:
            raise CliError("queue_unwritable", f"промпт не сохранился: {path} ({exc})",
                           {"path": str(path), "error": f"{type(exc).__name__}: {exc}"}) from exc
        return 200, "application/json", _json_bytes(
            {"ok": True, "name": name, "bytes": len(text.encode("utf-8")), "path": str(path)})

    # -- the chat editor: providers, the local model, sessions, turns -------------------------

    @staticmethod
    def _string_of(payload: dict, key: str) -> str:
        """`payload[key]` as a string, or `args_invalid`. Absent and `null` are both the empty
        string: the page sends an empty prompt window as `""` and an untouched one not at all, and
        neither is a mistake worth a refusal.
        """
        value = payload.get(key)
        if value is None:
            return ""
        if not isinstance(value, str):
            raise CliError("args_invalid", f"`{key}` must be a string",
                           {"field": key, "type": type(value).__name__})
        return value

    @staticmethod
    def _number_of(payload: dict, key: str, default):
        """`payload[key]` as a finite number, or `default` when the field is absent or `null`.

        Present and the wrong shape is `args_invalid`, the same rule `_string_of` applies to text
        fields: a chat `duration` reaches the system context verbatim as `duration: <value> s`
        (`_locked_turn`), and a malformed value there is a malformed instruction handed to the
        model, not a UI nuance this route can silently paper over. `bool` is excluded even though
        `isinstance(True, int)` is true in Python -- `true`/`false` are not seconds.

        **`NaN`/`Infinity`/`-Infinity` are excluded too** (review circle 1, fix round 1), even
        though all three are ordinary Python `float`s and `isinstance` alone cannot tell them from
        a real number. Python's own `json` module accepts those three tokens as an extension of
        the standard -- `json.loads("NaN")` is `float("nan")`, not a parse error -- so a
        hand-built request can put one on the wire without breaking anything before this check.
        `math.isfinite` is what actually excludes them.
        """
        value = payload.get(key)
        if value is None:
            return default
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value)):
            raise CliError("args_invalid", f"`{key}` must be a finite number",
                           {"field": key, "type": type(value).__name__})
        return value

    @staticmethod
    def _check_chat_duration(value) -> None:
        """`0 < value <= CHAT_DURATION_MAX`, or `args_invalid` -- the same refusal a `mode` the
        generator does not know already gets (fix round 1, review circle 1, Important).

        Runs on every `duration` this route computes, default included, but only a value this
        route itself just parsed from a request can ever fail it: `DEFAULT_CHAT_DURATION` (10) and
        every session's own last-known number were already checked the turn they were written, so
        the only way to reach a value outside the range is to send one in *this* request's body --
        `-5` (a browser's own `Number("-5") || 10` keeps a negative sign, it does not clear it),
        `0`, or `1e300` (finite, and `_number_of` alone has no opinion on "too large").
        """
        if not (0 < value <= CHAT_DURATION_MAX):
            raise CliError(
                "args_invalid",
                f"`duration` must be greater than 0 and at most {CHAT_DURATION_MAX}, and "
                f"{value} is not",
                {"value": value, "max": CHAT_DURATION_MAX})

    def _providers(self) -> tuple[int, str, bytes]:
        """`GET /api/providers`: the roster, exactly as the page may show it.

        Only the four keys the page needs, and no secret can reach this response even by accident:
        `load_providers` computes `available`/`reason` from the *name* of an .env variable and
        never copies its value (task 1), and nothing here forwards the rest of the config.
        """
        roster = provider.load_providers(self.server.outdir)
        listed = [{"name": name, "type": cfg.get("type"), "available": cfg.get("available"),
                   "reason": cfg.get("reason")} for name, cfg in roster["providers"].items()]
        return 200, "application/json", _json_bytes(
            {"ok": True, "active": roster["active"], "providers": listed})

    def _active_provider(self) -> tuple[str | None, dict]:
        """`(name, cfg)` of the active provider -- `(None, {})` when there is no roster at all.

        A missing `providers.json` is an ordinary state, not a failure: it is what this machine
        looks like before anyone configures a chat model, and every route here still has to answer.
        """
        roster = provider.load_providers(self.server.outdir)
        name = roster["active"]
        return name, roster["providers"].get(name) or {}

    def _llama_for(self, name: str | None, cfg: dict):
        """The `LlamaLocal` for one provider, or `None` when it is not a local one. External
        providers own no process, so there is nothing to be up or down and nothing to unload.
        """
        if cfg.get("type") != "llama-local":
            return None
        return provider.LlamaLocal(name, cfg, self.server.outdir)

    def _llm_state(self, name: str | None = None, cfg: dict | None = None) -> dict:
        """`{"status", "provider"}` for the model plate on the page.

        `down` for an external provider is not a lie by omission: the plate answers "are 31 GB of
        this machine's memory currently held by a chat model", and for openrouter the answer is no.

        Checks **every** `llama-local` provider in the roster (`provider.local_ports`), not only
        `name`/`cfg` (finding I1). The chat page's per-turn provider dropdown can raise a
        `llama-local` provider that is not `providers.json`'s `active` entry -- and `_locked_turn`
        passes exactly that provider's `name`/`cfg` here after a turn -- so a resident model
        elsewhere in the roster used to read as `down` whenever it was not the one this call
        happened to be about. `provider` in the answer names whichever entry is actually up,
        preferring `name` itself when it is the live one, so the common case (the provider this
        call is about *is* the resident model) still reads exactly as before the fix.
        """
        if cfg is None:
            name, cfg = self._active_provider()
        roster = provider.load_providers(self.server.outdir)
        alive_ports = {port for port in provider.local_ports(roster) if provider.port_alive(port)}
        if not alive_ports:
            return {"status": "down", "provider": name}
        if cfg.get("port") in alive_ports:
            return {"status": "up", "provider": name}
        for other_name, other_cfg in roster["providers"].items():
            if other_cfg.get("type") == "llama-local" and other_cfg.get("port") in alive_ports:
                return {"status": "up", "provider": other_name}
        return {"status": "up", "provider": name}  # unreachable: alive_ports came from this roster

    def _llm_status(self) -> tuple[int, str, bytes]:
        return 200, "application/json", _json_bytes({"ok": True, **self._llm_state()})

    def _llm_unload(self) -> tuple[int, str, bytes]:
        """`POST /api/llm/unload`: give the memory back before a generation needs it.

        Answers `down` even when there was nothing to stop -- the caller asked for a state, not for
        an action, and "there was no local provider" is not a failure to report.

        Shuts down **every** `llama-local` provider in the roster, not only the active one (finding
        I1): looking up `_active_provider()` alone missed a resident `gemma-local` whenever the
        active entry was external, and the human clicking "free the GPU" got nothing freed. One
        `LlamaLocal.shutdown()` call per distinct port (`provider.local_ports`) is enough -- its
        `pkill -f llama-server` already kills every local llama-server process on the machine
        regardless of which provider config it belongs to, and de-duplicating by port avoids
        calling it twice when two provider entries share one, as the real roster's do.
        """
        self._json_request(allowed=())
        roster = provider.load_providers(self.server.outdir)
        by_port: dict[int, tuple[str, dict]] = {}
        for pname, pcfg in roster["providers"].items():
            if pcfg.get("type") == "llama-local":
                by_port.setdefault(pcfg.get("port", 0), (pname, pcfg))
        for port in provider.local_ports(roster):
            pname, pcfg = by_port[port]
            provider.LlamaLocal(pname, pcfg, self.server.outdir).shutdown()
        return 200, "application/json", _json_bytes({"ok": True, "status": "down"})

    def _queue_pause(self) -> tuple[int, str, bytes]:
        """`POST /api/queue/pause`: mark the queue paused, so `main_loop`'s gate stops claiming.

        Never touches the job already in `running/` -- that gate sits *before* `claim`, never
        inside `run_job` (see `main_loop`'s docstring) -- so a pause requested mid-generation takes
        effect only for whatever the worker would have claimed next.
        """
        self._json_request(allowed=())
        with queue_errors(self.server.queue_root):
            q.set_paused(self.server.queue_root, True)
        return 200, "application/json", _json_bytes({"ok": True, "paused": True})

    def _queue_start(self) -> tuple[int, str, bytes]:
        """`POST /api/queue/start`: clear the pause marker, so `main_loop`'s gate lets `claim`
        through again on its next poll.
        """
        self._json_request(allowed=())
        with queue_errors(self.server.queue_root):
            q.set_paused(self.server.queue_root, False)
        return 200, "application/json", _json_bytes({"ok": True, "paused": False})

    def _chat_dir(self, create: bool = False) -> Path:
        """`<outdir>/chat/`, made only when something is about to be written into it.

        `create` is not a convenience flag, it is the whole point: this used to `mkdir` on every
        call, and `_chat_path` is on the *read* path too. A `GET /api/chat/<id>` for a session that
        does not exist -- what a page opened on a stale `/#chat/<id>` link does on its first
        request -- therefore left an empty `chat/` behind in the output directory. A read that
        writes is a small lie about state ("a chat lived here") and, on an outdir that is a mounted
        disk, a write nobody asked for.
        """
        directory = Path(self.server.outdir) / CHAT_DIR
        if create:
            directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _chat_path(self, sid: str, create: bool = False) -> Path:
        """`<outdir>/chat/<sid>.json`, or `path_outside_root`.

        The id arrives in a URL and becomes a filename, so it is a path component in everything but
        name and is checked the way `_job_id_of` checks a job id: resolve it, then require the
        result to be one file directly inside the chat directory. `../../etc/passwd` and `sub/dir`
        are both refused before anything opens a file, and the refusal carries a code rather than
        being a 404 indistinguishable from an unrecognised URL.

        **Too long to be a filename is decided here, not by the kernel.** It used to fall out of
        the `ENAMETOOLONG` the first `open` raised (`name_too_long_is_a_refusal`), which made the
        answer depend on something that has nothing to do with the id: once `chat/` stopped being
        `mkdir`-ed on the read path, lookup failed at the *missing directory* with `ENOENT` first
        and a 400-character id came back as an ordinary «нет сессии» 404. The id's own shape is
        knowable without touching the filesystem, so it is judged without touching it, and the
        kernel's own refusal stays as the backstop for the limits this does not know.
        """
        name = f"{sid}.json"
        # Both numbers in the sentence are bytes, deliberately. The limit is a byte limit, and a
        # Cyrillic id is two bytes a character: quoting the *character* count beside it read as
        # «210 is over 255» and sent whoever hit it looking for a second rule.
        name_bytes = len(name.encode("utf-8", "surrogatepass"))
        if name_bytes > NAME_MAX_BYTES:
            raise CliError(
                "path_outside_root",
                f"a chat id has to fit in a filename of {NAME_MAX_BYTES} bytes, and this one "
                f"needs {name_bytes} (id: {len(sid)} characters)",
                {"id": sid[:80], "chars": len(sid), "bytes": name_bytes,
                 "limit": NAME_MAX_BYTES},
            )
        directory = self._chat_dir(create=create)
        target = resolve_within(directory / f"{sid}.json", {"chat": directory}, write=True)
        if target.parent != directory.expanduser().resolve() or target.suffix != ".json":
            raise CliError(
                "path_outside_root",
                f"a chat id names one file in the chat directory, and {sid!r} does not",
                {"id": sid, "resolved": str(target)},
            )
        return target

    def _chat_image_path(self, raw: str) -> Path:
        """A keyframe path from a session or a request body: inside a root, and an image.

        Neither check is a formality. The bytes of this file are base64'd into a chat turn and sent
        to whichever provider is active -- possibly one on the internet -- so an unchecked path
        turns one POST into "read any file on this machine and upload it". `--image` is a `read`
        flag in `PATH_FLAGS` for a run; a chat about that same frame cannot be laxer.

        And the root bound alone is not enough, which is what review circle 1 found: `<outdir>/.env`
        is *inside* a root and holds the provider tokens. The type allowlist is the second question
        (`CHAT_IMAGE_SUFFIXES`), asked of the **resolved** path, so a link named `frame.png`
        pointing at `.env` is judged as `.env` -- the same rule, and the same reasoning, as
        `_serve_file`.

        Resolved on the way out, like `check_path_flags` does, so the session stores the path the
        server will actually open rather than one that means something else from another directory.
        """
        target = resolve_within(raw, self.server.roots, write=False)
        if target.suffix.lower() not in CHAT_IMAGE_SUFFIXES:
            raise CliError(
                "bad_image",
                f"кадром может быть только {sorted(CHAT_IMAGE_SUFFIXES)}, а {target.name!r} — нет",
                {"path": str(raw), "suffix": target.suffix,
                 "allowed": sorted(CHAT_IMAGE_SUFFIXES)},
            )
        return target

    def _create_chat(self) -> tuple[int, str, bytes]:
        """`POST /api/chat`: one new session on disk, and its id.

        The id is `secrets.token_hex(4)` -- the queue's own `_suffix()` shape, unguessable and
        short enough to read out of a URL. Written durably for the same reason every other file
        this server confirms is: the page navigates to `/#chat/<id>` the moment it gets the id, and
        a session that is not on disk by then is a page that opens on a 404.
        """
        payload = self._json_request(
            allowed=("source", "prompt", "mode", "image", "end_image", "duration"))
        source = payload.get("source") or {"kind": "new"}
        if not isinstance(source, dict) or source.get("kind") not in CHAT_SOURCE_KINDS \
                or not set(source) <= CHAT_SOURCE_KEYS:
            raise CliError(
                "args_invalid",
                f"`source` is an object with `kind` in {sorted(CHAT_SOURCE_KINDS)} and nothing "
                f"outside {sorted(CHAT_SOURCE_KEYS)}",
                {"source": source if isinstance(source, (dict, str)) else None,
                 "kinds": sorted(CHAT_SOURCE_KINDS), "keys": sorted(CHAT_SOURCE_KEYS)},
            )
        # The same closed-list check as `source.kind` above, and refused with the same code: a
        # session's `mode` is read by the model and by the page, and neither can tell a typo from
        # a mode it has not learned yet. Empty (and absent) stays valid -- `DEFAULT_CHAT_MODE`
        # answers for it at turn time.
        mode = self._string_of(payload, "mode")
        if mode and mode not in CHAT_MODES:
            raise CliError(
                "args_invalid",
                f"`mode` is one of {sorted(CHAT_MODES)} or empty, and {mode!r} is not",
                {"mode": mode, "modes": sorted(CHAT_MODES)},
            )
        image = self._string_of(payload, "image")
        # `end_image` (T4): the last-frame keyframe of a `flf` job. Checked with the same rule as
        # `image` -- inside a root, and an image suffix -- because it names a file on disk exactly
        # the same way. Unlike `image`, its bytes are never attached to a turn (`_locked_turn` only
        # mentions the path in the system context): the model does not need to *see* the last
        # frame to reason about it, and sending it would double the per-turn upload for no benefit.
        end_image = self._string_of(payload, "end_image")
        # `duration` (A3): the seconds the modal's parse (`analysePrompt`, on the page) checks
        # shot cuts against. The page decides *which* number to send -- `#duration` on a session
        # opened from the form, `--duration` out of a job's own `args` on one opened from the
        # queue (`openChatFromJob`, the same client-side pattern `mode`/`image`/`end_image` use) --
        # this route only stores whatever number arrives, defaulting the same way an absent
        # `mode` does.
        duration = self._number_of(payload, "duration", DEFAULT_CHAT_DURATION)
        self._check_chat_duration(duration)
        session = {"id": secrets.token_hex(4),
                   "source": source,
                   "mode": mode,
                   "image": str(self._chat_image_path(image)) if image else "",
                   "end_image": str(self._chat_image_path(end_image)) if end_image else "",
                   "duration": duration,
                   "messages": [],
                   "prompt": self._string_of(payload, "prompt")}
        self._write_session(self._chat_path(session["id"], create=True), session)
        return 200, "application/json", _json_bytes({"ok": True, "id": session["id"]})

    @staticmethod
    def _write_session(path: Path, session: dict) -> None:
        """The session file, written durably -- the same protocol, and the same refusal on failure,
        as `_save_prompt`. Durable because the page navigates to the session as soon as it has the
        id, and a half-written file is a conversation that opens truncated.
        """
        try:
            q.write_json_durably(path, session)
        except OSError as exc:
            raise CliError("queue_unwritable", f"сессия не сохранилась: {path} ({exc})",
                           {"path": str(path), "error": f"{type(exc).__name__}: {exc}"}) from exc

    def _read_session(self, sid: str) -> tuple[Path, dict | None]:
        """`(path, session)` for `sid`, with `session` `None` when there is no such file.

        `ENAMETOOLONG` is a refusal naming the id (`name_too_long_is_a_refusal`), not a 500: a
        400-character id in a URL is the caller's input, exactly as it is for a job id.
        """
        path = self._chat_path(sid)
        try:
            with name_too_long_is_a_refusal("a chat id"):
                session = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return path, None
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            raise CliError("queue_unwritable", f"сессия не читается: {path} ({exc})",
                           {"id": sid, "path": str(path)}) from exc
        _check_session_shape(sid, path, session)
        return path, session

    def _read_chat(self, sid: str) -> tuple[int, str, bytes]:
        path, session = self._read_session(sid)
        if session is None:
            return 404, "application/json", _error_bytes(
                "chat_not_found", f"нет сессии {sid}", {"id": sid})
        return 200, "application/json", _json_bytes({"ok": True, **session})

    def _delete_chat(self, sid: str) -> tuple[int, str, bytes]:
        """`DELETE /api/chat/<id>`: the session file and its lock, gone -- «очистить» in the
        modal's head.

        **Refused with `chat_busy`, not queued behind the turn.** `chat_session_lock` is the same
        non-blocking guard `_chat_message` takes for a turn: a turn in flight holds it, so trying
        to delete out from under a read-modify-write in progress fails immediately with 409 rather
        than waiting a minute and then deleting a file the model is mid-write on. Nothing is
        unlinked before the lock is granted, so a refused delete leaves both files exactly as they
        were.

        **The lock file too, not only the session.** `chat_session_lock`'s own docstring explains
        why `<sid>.lock` outlives every turn it guards: it is never touched by `os.replace`, only
        `flock`ed and released. Left behind, a `.lock` with no `.json` beside it is a lock some
        *other* session id -- generated later by `secrets.token_hex(4)` reusing the same eight
        hex digits -- would silently inherit, and finding out would take a very unlucky day.

        **Existence is checked before the lock is taken**, the same order `_chat_message` uses:
        an id nobody ever created should answer `chat_not_found`, not `chat_busy`, and the
        `is_file` check inside `name_too_long_is_a_refusal` never creates the lock file the way
        entering `chat_session_lock` itself would.
        """
        path = self._chat_path(sid)
        with name_too_long_is_a_refusal("a chat id"):
            exists = path.is_file()
        if not exists:
            return 404, "application/json", _error_bytes(
                "chat_not_found", f"нет сессии {sid}", {"id": sid})
        with chat_session_lock(path):
            path.unlink(missing_ok=True)
            path.with_suffix(".lock").unlink(missing_ok=True)
        return 200, "application/json", _json_bytes({"ok": True})

    def _turn_content(self, text: str, image: str):
        """The user's turn, and a warning: plain text, or `[text, image_url]` plus `None`.

        The two parts are what a vision model needs to see the frame, and the same shape works for
        llama-server with an mmproj and for the external OpenAI-protocol providers.

        **Anything wrong with the frame is a warning, not a refusal.** The file may have been moved
        since the session was opened, or the session may be older than a rule this server has since
        learned; refusing the turn would throw away text the person already wrote for a reason that
        has nothing to do with it. The warning is `{"code", "message"}` rather than a sentence, so
        the page matches on the code like it does everywhere else, and writes it into the transcript
        -- nobody should spend a turn wondering why the model is describing a frame it never got.

        **The checks are repeated here even though `_create_chat` already made them.** A session
        lives on disk between the two, and the file it names can change under it; the decision to
        send bytes belongs where the bytes are read.

        The image never enters the session's history -- it is attached again on every turn instead.
        A base64 PNG in `messages` would grow the session file by a megabyte per turn and would be
        re-sent by every later turn anyway.
        """
        if not image:
            return text, None
        try:
            path = self._chat_image_path(image)
            size = path.stat().st_size
            if size > CHAT_IMAGE_MAX_BYTES:
                raise CliError(
                    "bad_image",
                    f"кадр больше {CHAT_IMAGE_MAX_BYTES} байт ({size}): {image}",
                    {"path": image, "bytes": size, "limit": CHAT_IMAGE_MAX_BYTES})
            data = path.read_bytes()
        except CliError as exc:
            return text, {"code": exc.code, "message": exc.message}
        except FileNotFoundError:
            return text, {"code": "image_not_found", "message": f"кадр не прочитался: {image}"}
        except OSError as exc:
            return text, {"code": "image_unreadable",
                          "message": f"кадр не прочитался: {image} ({exc})"}
        url = f"data:{_content_type(path)};base64,{base64.b64encode(data).decode('ascii')}"
        return [{"type": "text", "text": text},
                {"type": "image_url", "image_url": {"url": url}}], None

    def _chat_message(self, sid: str) -> tuple[int, str, bytes]:
        """`POST /api/chat/<id>/message`: one turn, synchronously.

        Synchronous on purpose: `ensure_up` can take a minute on a cold model, and the alternative
        -- a job, a poll, a second state machine -- is a great deal of machinery for a page that
        has nothing else to do while it waits.

        **The queue outranks the chat.** A local model holds 31 GB, so raising one while a
        generation is running is how both die; the turn is refused with `gpu_busy` and, since the
        refusal must leave nothing behind, the message is not written to the session either. An
        external provider takes none of this machine's memory and is therefore not refused -- the
        check is on the provider's kind, not on the queue alone.

        **The prompt in the system message comes from the request body, never from the session.**
        The person may have edited the text in the window since the last turn, and a model shown
        the saved copy would "restore" every hand edit it did not know about.

        A `ProviderError` becomes a 502 carrying the provider's own code (`chat_unreachable`,
        `bad_model_json`, `llama_did_not_start`). That mapping is this method's job and not
        `provider`'s: the domain layer raises a named failure, and the HTTP boundary decides what
        that is worth as a status -- the same shape as `queue.JobNotPending` -> `CliError` above.

        **One turn of one session at a time** (`chat_session_lock`): the whole read-modify-write,
        the model call included, happens under the session's own lock, and a second turn of the
        same session is refused with `chat_busy` before it costs anything. The body is parsed
        first, so a malformed request is still a 400 rather than a lock contention report; the
        session is re-read *inside* the lock, because the copy read before it may be a turn out of
        date by the time the lock is granted.

        **`image`/`set_mode` (A8): a keyframe dropped on the chat's own input, mid-conversation.**
        The page uploads the file first (`POST /api/uploads`, A7) and hands this route the path
        it got back, in the same turn as the text -- one user action, one request, exactly the way
        `mode`/`image` arrive together at `_create_chat`. Both are optional and both update the
        session (`_locked_turn`) before this turn's own system/user messages are built, so the
        frame the model is shown and the frame the session remembers afterward are the same one.
        """
        path = self._chat_path(sid)
        # `is_file` before the lock, so a bogus id does not leave a `.lock` file behind -- and
        # inside `name_too_long_is_a_refusal`, because `pathlib` swallows `ENOENT` but not
        # `ENAMETOOLONG`, and a 400-character id in a URL is the caller's input, not a bug here.
        with name_too_long_is_a_refusal("a chat id"):
            exists = path.is_file()
        if not exists:
            return 404, "application/json", _error_bytes(
                "chat_not_found", f"нет сессии {sid}", {"id": sid})
        payload = self._json_request(
            allowed=("text", "prompt", "provider", "duration", "image", "set_mode"))
        with chat_session_lock(path):
            return self._locked_turn(sid, path, payload)

    def _locked_turn(self, sid: str, path: Path, payload: dict) -> tuple[int, str, bytes]:
        """One turn, with this session's lock already held. See `_chat_message`."""
        _, session = self._read_session(sid)
        if session is None:
            return 404, "application/json", _error_bytes(
                "chat_not_found", f"нет сессии {sid}", {"id": sid})
        text = self._string_of(payload, "text")
        # `image` (A8): a keyframe the person just dropped on the chat's own input, not one that
        # has been sitting in the session since it opened. That difference is why this is a hard
        # refusal (`_chat_image_path` raises straight through, uncaught) rather than folded into
        # `_turn_content`'s warning below -- the warning is for a frame that *used to* be valid and
        # stopped being one between turns; this is a path the person handed the server this very
        # second, and an unreadable or wrong-suffix one is a mistake worth stopping the turn for,
        # not a footnote on a reply they never asked to send it without a picture. Checked, and the
        # session updated, before anything about this turn reaches the model or `_write_session`:
        # a refusal here must leave the session exactly as it was (see `_chat_message`'s docstring
        # on `image`/`set_mode`), which holds for free as long as nothing is written before it.
        image = self._string_of(payload, "image")
        if image:
            session["image"] = str(self._chat_image_path(image))
        # `set_mode` (A8): rides the same turn as the frame, because a dropped image and the mode
        # it implies are one user action -- the same closed list `mode` is checked against at
        # `_create_chat`, refused the same way (`args_invalid`), for the same two readers (the
        # model's `## Context` line, and the page's own sound-section guess).
        set_mode = self._string_of(payload, "set_mode")
        if set_mode and set_mode not in CHAT_MODES:
            raise CliError(
                "args_invalid",
                f"`set_mode` is one of {sorted(CHAT_MODES)} or empty, and {set_mode!r} is not",
                {"set_mode": set_mode, "modes": sorted(CHAT_MODES)},
            )
        if set_mode:
            session["mode"] = set_mode
        # `duration` (A3): editable in the modal's own header (`chat-duration`), and re-sent with
        # every turn -- a person may move the number after the session opened, and the next turn
        # has to see what is in the field *now*, the same reasoning `_locked_turn`'s own docstring
        # gives for `prompt` never coming from the saved session. Absent from this turn's body
        # (a request built by hand, or an older page), the session's own last-known number stands;
        # only a session that has never been told one at all falls back to the ten-second default.
        duration = self._number_of(payload, "duration",
                                   session.get("duration", DEFAULT_CHAT_DURATION))
        self._check_chat_duration(duration)
        session["duration"] = duration
        roster = provider.load_providers(self.server.outdir)
        name = self._string_of(payload, "provider") or roster["active"]
        cfg = roster["providers"].get(name)
        if not cfg or not cfg.get("available"):
            return 409, "application/json", _error_bytes(
                "provider_unavailable",
                (cfg or {}).get("reason")
                or (f"нет провайдера {name}" if name else "активный LLM-провайдер не выбран"),
                {"provider": name})
        lam = self._llama_for(name, cfg)
        if lam is not None:
            running = _generation_running(self.server.queue_root)
            if running:
                return 409, "application/json", _error_bytes(
                    "gpu_busy", "идёт прогон — модель поднимется после него",
                    {"running": _running_ids(running)})
        # `end_image` (T4): mentioned by path only, never attached as a picture -- the model needs
        # to know a last frame exists (so it writes the `mode: flf` instruction line and describes
        # a path toward it, per docs/h3-prompt-system.md) without paying for a second image upload
        # every turn the way `_turn_content` pays for the first frame.
        end_image_line = (f"\nend_image: {session['end_image']}" if session.get("end_image")
                          else "")
        # Task 5 ("Проекты"): `kind` -- present only once a turn of *this* session has actually
        # answered with a `project` object (see below), never set at creation the way `mode` is.
        # A project's `kind` (`video`/`clip`/`song`) is something the model decides while talking
        # to the person, not something the page can know before the conversation starts -- so the
        # line is absent, not a placeholder, until there is a real answer to put there. Once
        # present, it rides every later turn's context so the model does not lose track of which
        # kind of project it already committed this session to.
        kind_line = f"\nkind: {session['kind']}" if session.get("kind") else ""
        system = (provider.system_prompt()
                  + "\n\n## Context\nmode: " + (session.get("mode") or DEFAULT_CHAT_MODE)
                  + f"\nduration: {duration:g} s"
                  + kind_line
                  + end_image_line
                  + "\n\n## Current prompt\n" + self._string_of(payload, "prompt"))
        content, warning = self._turn_content(text, session.get("image") or "")
        messages = ([{"role": "system", "content": system}]
                    + [{"role": message["role"], "content": message["content"]}
                       for message in session["messages"]]
                    + [{"role": "user", "content": content}])
        try:
            if lam is not None:
                lam.ensure_up()
            turn = provider.chat(cfg, provider.load_env(self.server.outdir), messages)
        except provider.ProviderError as exc:
            return 502, "application/json", _error_bytes(exc.code, str(exc), {"provider": name})
        if not isinstance(turn, dict):
            # `null`, a bare string or a list are all valid JSON and none of them is a turn.
            # `provider.chat` parses rather than validates, so the shape is checked once here,
            # where the alternative is `None.get("reply")` reaching the `internal_error` net and
            # reporting the model's mistake as a bug in this server.
            return 502, "application/json", _error_bytes(
                "bad_model_json", f"модель вернула не объект: {type(turn).__name__}",
                {"provider": name, "type": type(turn).__name__})
        reply = turn.get("reply")
        if not isinstance(reply, str):
            # The same question as `isinstance(turn, dict)` above, asked one level in, and asked
            # here rather than papered over with `str()` for two reasons.
            #
            # It is a *format* failure, and `bad_model_json` is what that is called: the schema
            # says `reply` is a string, and a provider that ignores `response_format` (external
            # ones do -- the schema is a request, not a guarantee) answers `{"reply": 42}`.
            # `str(42)` would put "42" in the transcript as something the model said.
            #
            # And without the check the number reached `messages` as a `content`, the turn
            # answered 200, and the *next* read hit `_check_session_shape`, which correctly
            # refused a message whose `content` is not a string. From then on the session was
            # dead: `chat_corrupt` on every GET and every further turn, unfixable without an
            # editor -- a file this server wrote itself and then declared hand-damaged. A
            # refusal that leaves nothing behind cannot do that; see `chat_busy` on why the
            # refusal must be taken before anything is written.
            return 502, "application/json", _error_bytes(
                "bad_model_json",
                f"модель ответила не текстом: `reply` пришёл как {type(reply).__name__}",
                {"provider": name, "type": type(reply).__name__})
        # Only the text of the turn is kept: see `_turn_content` on why the frame is not.
        session["messages"] += [{"role": "user", "content": text},
                                {"role": "assistant", "content": reply}]
        if turn.get("prompt"):
            session["prompt_struct"] = turn["prompt"]
        # A4: `slug` is optional metadata (`PROMPT_SCHEMA`'s own `required` leaves it out), and
        # deliberately held to a looser standard than `reply` a few lines up. `reply` is
        # structural -- it becomes `messages[-1]["content"]`, and a wrong type there bricks the
        # session the moment `_check_session_shape` reads it back, which is why it earns a 502.
        # `slug` never touches `messages` or anything shape-checked; a model that ignores
        # `response_format` and sends `slug: 42` costs nothing to treat as if it had sent nothing
        # at all. So it is: not saved, not echoed, and the turn still answers 200 -- the same
        # "absent is not an error" the field's own place outside `required` already promises,
        # whether the absence is real or just a type this server declined to trust.
        slug = turn.get("slug")
        if isinstance(slug, str) and slug:
            session["slug"] = slug
        else:
            slug = None
        # Task 5 ("Проекты"): `project` follows the same rule `slug` just did -- optional,
        # outside `PROMPT_SCHEMA`'s `required`, and a provider outside `response_format`'s reach
        # can send anything, so a malformed one is ignored quietly rather than earning a 502.
        # Saved whole under `project` (mirrors `prompt_struct` holding the single-clip `prompt`),
        # and `kind` besides it at the session's top level -- "по образцу", the same place
        # `mode`/`duration` already live -- so a later turn's `## Context` line above, and Task 6's
        # project-creation route, can both read the current kind without reaching into `project`.
        # Neither is set at session creation the way `mode` is (see `kind_line`'s own comment
        # above): the model decides a project's `kind` while talking to the person, this route
        # only ever records what it decided.
        project = turn.get("project")
        if isinstance(project, dict):
            session["project"] = project
            kind = project.get("kind")
            if isinstance(kind, str) and kind in PROJECT_KINDS:
                session["kind"] = kind
            else:
                # Review M2: `session["project"]` was just overwritten wholesale (above), so a
                # `kind` a previous turn set must not go on claiming to describe *this* `project`
                # -- leaving it in place would let `session["kind"]` and
                # `session["project"]["kind"]` disagree the moment this turn's own `kind` is
                # missing or unrecognised, exactly the pair Task 6's project-creation route reads
                # as if they always matched.
                session.pop("kind", None)
        else:
            project = None
        self._write_session(path, session)
        return 200, "application/json", _json_bytes(
            {"ok": True, "reply": reply, "prompt": turn.get("prompt"), "slug": slug,
             "project": project, "warning": warning, "llm": self._llm_state(name, cfg)})

    def _media(self, relative: str) -> tuple[int, str, bytes]:
        """A preview frame or a finished clip from **one** run's directory somewhere inside the
        outdir -- at any depth, not just a direct child.

        Fix round 1 (task A6 review, C1): a job's own `--outdir` can already sit more than one
        level inside the server's own outdir (`defaultOutdir()` in `app.js` defaults the form to
        `~/video-out/<date>`, one level down on its own; task A6's own per-job subdirectory adds a
        second), so "the run directory" can no longer mean "the first URL segment, a direct child
        of the outdir" -- the page cannot even name that one segment without knowing where the
        server's own root ends, which is why `/api/state` now carries `outdir` (`build_state`).
        This route follows: "the run directory" is now *everything before the last `/`*, and the
        file is everything after it.

        Five checks, run in this order:

        1. there must be a run segment and a file after it (`rpartition`'s own `not separator`);
        2. no component of the **literal, unresolved** run path may be `.`, `..` or empty. This is
           new here and it is not optional now that the run path can be more than one segment:
           `resolve_within` alone answers "does this resolve inside the outdir", and
           `run-a/../run-b` resolves to a path that is perfectly, legitimately inside the outdir --
           `run-b`'s own directory -- while still being exactly the escape-from-one-run-into-
           another review circle 1 first found (`test_media_cannot_step_out_of_one_run_into_
           another`). The three collapsing spellings that check found (`//x`, `/./x`, `/%2e/x`)
           are refused by the same rule: each puts an empty or `.` component in the run path;
        3. `<outdir>/<run>` must resolve inside the outdir -- this is what stops a run path that
           starts with enough literal `..` components to leave it entirely (`../../etc/passwd`);
        4. it must not *be* the outdir itself, and it must not be the queue or anything inside the
           queue, decided by inode identity (`_is_within`) at every level from the run directory up
           to the outdir -- not by comparing resolved path text, which is wrong on a case-
           insensitive volume (`_is_same_file`'s own docstring) and was already wrong before this
           fix for the exact-match case; walking every ancestor is what makes `queue/logs/x.jpg`
           refused exactly as `queue` itself already was, now that "inside the queue" is not
           bounded to one level either;
        5. and the file itself must be one of `MEDIA_SUFFIXES` (in `_resolve_servable`), which is what
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

        The tail after the run path is deliberately *not* flattened further: a run directory has
        subdirectories of its own (`checkpoints/`), and everything below it is still inside it --
        `19-real-run/checkpoints/step05.jpg` is a legitimate preview frame, and so, now, is
        `2026-08-12/20260813-1435-kot-italy/h3-kot-italy-896x576.mp4`.

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
        directory, separator, filename = relative.rpartition("/")
        if not separator or not filename:
            return 404, "application/json", _error_bytes(
                "not_found", "a media URL is /media/<run>/<file>", {"path": relative})
        if any(part in ("", ".", "..") for part in directory.split("/")):
            raise CliError(
                "path_outside_root",
                f"/media does not accept an empty, . or .. segment in a run's own path: "
                f"{relative!r}",
                {"path": relative, "run": directory},
            )
        outdir = Path(self.server.outdir).resolve()
        run_dir = resolve_within(Path(self.server.outdir) / directory, {"outdir": outdir},
                                 write=False)
        if run_dir == outdir or _is_within(run_dir, self.server.queue_root):
            raise CliError(
                "path_outside_root",
                f"/media serves one run's directory inside the output directory, and {directory!r} "
                f"is not one",
                {"path": relative, "run": directory, "resolved": str(run_dir),
                 "outdir": str(outdir)},
            )
        target = _resolve_servable(run_dir, filename, suffixes=MEDIA_SUFFIXES)
        if target is None:
            return 404, "application/json", _error_bytes(
                "not_found", f"no such file: {filename}", {"path": relative})
        try:
            total = target.stat().st_size
        except OSError as exc:
            return 404, "application/json", _error_bytes(
                "not_found", f"no such file: {filename}",
                {"path": relative, "error": f"{type(exc).__name__}: {exc}"})
        return self._range_response(target, total, relative)

    def _range_response(self, target: Path, total: int, relative: str) -> tuple[int, str, bytes]:
        """The 200/206/416 answer for one `/media` file already resolved (by `_media`) and sized,
        honouring a single `Range: bytes=...` request.

        This is the whole fix for the bug that started task 2: Safari's `<video>` tag probes a
        source with `Range: bytes=0-1` before it will show a poster frame at all, and this route
        used to answer every request -- probe included -- with a flat 200 and the entire file.
        Safari never got the 206 it was waiting for, so the first frame in a finished card's video
        tag stayed blank until playback was pressed by hand.

        `self._extra_headers["Accept-Ranges"] = "bytes"` is set on **every** answer this method
        gives -- 200, 206 and 416 alike -- because all three are honest statements about a file
        this route resolved and sized; `_send` writes it alongside the fixed three headers every
        response already carries, and `_respond` resets `self._extra_headers` before each request
        so it cannot leak onto an unrelated response on the same keep-alive connection.

        `_parse_range` (see its own docstring for the RFC 7233 reasoning) decides the outcome:

        * `None` -- no `Range`, or one this route ignores -- the ordinary whole-file 200.
        * `RANGE_UNSATISFIABLE` -- `range_not_satisfiable`, mapped to 416, with
          `Content-Range: bytes */<total>` so the client learns the real length without any body.
        * `(start, end)` -- 206, `Content-Range: bytes <start>-<end>/<total>`, and exactly that
          slice of bytes, read with a seek rather than `target.read_bytes()` -- the two-byte probe
          that started this task must cost two bytes of I/O, not the whole clip.

        `OSError` (a permission bit flipped between `_media`'s `stat()` and this method's own
        `open`/`read`, the same race `_serve_file` already answers with 404 rather than a 500) is
        caught around both read paths and turned into the same `not_found` `_serve_file` gives --
        an unreadable file must not be an oracle for "this file exists but you may not have it".
        """
        self._extra_headers["Accept-Ranges"] = "bytes"
        range_header = self._sole_header("Range", "bad_request")
        outcome = _parse_range(range_header, total)
        if outcome is RANGE_UNSATISFIABLE:
            self._extra_headers["Content-Range"] = f"bytes */{total}"
            raise CliError(
                "range_not_satisfiable",
                f"the requested Range {range_header!r} is outside the {total}-byte file",
                {"path": relative, "total": total, "range": range_header},
            )
        try:
            if outcome is None:
                return 200, _content_type(target), target.read_bytes()
            start, end = outcome
            with open(target, "rb") as handle:
                handle.seek(start)
                body = handle.read(end - start + 1)
            self._extra_headers["Content-Range"] = f"bytes {start}-{end}/{total}"
            return 206, _content_type(target), body
        except OSError as exc:
            return 404, "application/json", _error_bytes(
                "not_found", f"no such file: {relative}",
                {"path": relative, "error": f"{type(exc).__name__}: {exc}"})

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

    def _cache_control(self, status: int) -> str:
        """`no-store` for everything, and one exception: a file this route actually served
        from a run directory.

        `no-store` is the right default and stays the default -- the page is polled every 20
        seconds and the queue changes under it, so a cached `/api/state` would show a worker
        that stopped an hour ago, and a cached `app.js` would outlive the `h3 web` restart that
        shipped a new one.

        The exception is the one place where it cost rather than bought (task C3, C2 review):
        the page redraws its result cards on every poll and writes the same `<img src>` back
        into the DOM, so under blanket `no-store` an evening of ten finished runs re-fetched ten
        preview frames three times a minute for bytes that cannot have changed. `MEDIA_MAX_AGE`
        says why they cannot.

        **Only 200 or 206, and only under `/media`.** A refusal is never cached, and the case
        that makes that matter rather than a formality is the 404: the commonest one this route
        answers is a preview frame the run has not written *yet*, and a cached one would keep
        that card blank for the rest of the evening -- the browser would stop asking, and the
        frame that appeared two minutes later would never be fetched. 206 joins 200 (task 2's
        Range support): a byte range out of a run's own clip is exactly as immutable as the whole
        file it was cut from -- same path, same never-rewritten-in-place guarantee -- and Safari
        re-issues its own opening `bytes=0-1` probe on every card redraw exactly like the `<img>`
        tags this exception already exists for. 416 is deliberately excluded: it carries no bytes
        of the file at all, just `Content-Range: bytes */<total>`, and caching that would freeze a
        stale `<total>` in the browser the next time the same URL is asked for a real range.

        The path is unquoted first, exactly as `_route_get` unquotes it before matching, so this
        answer cannot disagree with the route that produced the body. `getattr` because
        `send_error` reaches `_send` on a request line the base class failed to parse, where
        there is no `self.path` at all.
        """
        if status not in (200, 206):
            return "no-store"
        path = urllib.parse.unquote(urllib.parse.urlsplit(getattr(self, "path", "")).path)
        if not path.startswith("/media/"):
            return "no-store"
        return f"public, max-age={MEDIA_MAX_AGE}, immutable"

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        """One place that writes a response, so `Content-Length` cannot be forgotten on one path.

        `self._extra_headers` (`Accept-Ranges`, `Content-Range` -- see `_media`'s
        `_range_response`) is read with `getattr`, not a bare attribute access: `send_error` can
        reach this method on a request line the base class failed to parse, before `_respond` ever
        ran and set the dict up, and a response to *that* carries no extra headers at all.
        """
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", self._cache_control(status))
        for name, value in getattr(self, "_extra_headers", {}).items():
            self.send_header(name, value)
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
                verbose=False, reveal=None) -> ThreadingHTTPServer:
    """A server bound to the loopback, ready for `serve_forever()`.

    `port=0` asks the kernel for a free one; the actual number is in `server_address[1]`, which is
    how the tests reach it without racing over a fixed port.

    `repo`, `models` and `webui` are parameters rather than module constants read at call time so a
    test can point them at a temporary tree -- but they default to the real ones, so nothing in
    production depends on a caller getting them right.

    `reveal` is the same idea for `_reveal_job`'s `open -R`: a callable of one `Path`, defaulting
    to `_reveal_in_finder`, which is the only thing in this module that ever shells out for it.
    A test that wants to assert *what* would be revealed, without a real Finder or a real macOS
    box, passes its own here (see `tests/test_chat_web.py`'s `_serve`).
    """
    httpd = _Server((LOOPBACK, port), _Handler)
    # Built from the port the socket actually got, not from `port`: with `port=0` the kernel picks,
    # and a set built from the request would reject every request the server then received.
    bound = httpd.server_address[1]
    httpd.allowed_hosts = frozenset({f"{LOOPBACK}:{bound}", f"localhost:{bound}"})
    # The same two names as an origin, for the write routes. Built from `allowed_hosts` so the two
    # sets cannot drift apart, and `http://` because this server has no TLS and never will.
    httpd.allowed_origins = frozenset(f"http://{host}" for host in httpd.allowed_hosts)
    httpd.queue_root = Path(queue_root)
    httpd.outdir = Path(outdir)
    httpd.webui = Path(webui) if webui is not None else WEBUI_ROOT
    httpd.roots = {"repo": Path(repo) if repo is not None else REPO_ROOT,
                   "outdir": Path(outdir),
                   "models": Path(models) if models is not None else models_root()}
    httpd.verbose = verbose
    httpd.reveal = reveal if reveal is not None else _reveal_in_finder
    return httpd
