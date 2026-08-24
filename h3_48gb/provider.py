"""LLM providers for the chat prompt editor.

Two kinds speak one protocol (OpenAI /v1/chat/completions): `llama-local`
also owns the llama-server process lifecycle, `openai` only needs a URL and
a token. Tokens never live in providers.json -- only the *name* of an .env
variable does, so the roster can be shown to the page verbatim.
"""
from __future__ import annotations

import http.client
import json
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from h3_48gb.project import PROJECT_KINDS


class ProviderError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# The model cannot answer outside this shape: llama.cpp enforces it with a
# grammar compiled from the schema, external providers via response_format.
PROMPT_SCHEMA = {
    "name": "h3_chat_turn",
    "schema": {
        "type": "object",
        "properties": {
            "reply": {"type": "string"},
            "prompt": {
                "type": ["object", "null"],
                "properties": {
                    "instruction": {"type": ["string", "null"]},
                    "integrated_multimodal_description": {"type": "string"},
                    "overall_soundscape": {"type": "string"},
                    "non_diegetic_music": {"type": "string"},
                },
                "required": ["instruction", "integrated_multimodal_description",
                             "overall_soundscape", "non_diegetic_music"],
                "additionalProperties": False,
            },
            # A4: a short slug for the scene (`cat-italian-noon`), used to name the run instead of
            # the generic "run" tag. Optional and outside `required` on purpose -- every response
            # this schema ever produced before A4 had no such key, and a schema that suddenly
            # demanded one would make every one of those old, already-saved turns invalid.
            "slug": {"type": ["string", "null"]},
            # Task 5 ("Проекты"): a scenario -- a scripted multi-scene video, or lyrics+caption for
            # a song/clip -- alongside (never instead of) the single-clip `prompt` above. Optional
            # and outside `required` for the exact same backward-compatibility reason `slug` is:
            # every ordinary video-editing turn this schema ever produced, before this project
            # field existed, carried no such key, and a schema that suddenly demanded one would
            # make every one of those turns invalid. `kind` mirrors `h3_48gb.project.PROJECT_KINDS`
            # (the single source of truth a `project.json` on disk already uses) rather than a
            # second, independently-spelled list living here -- see the import at the top of this
            # module. `scenes`/`lyrics`/`caption` are all nullable and all always present (the same
            # "every key required, unwanted ones null" shape `prompt`'s own fields already use):
            # a `kind: "video"` answer sets `scenes` and leaves `lyrics`/`caption` null; a
            # `kind: "clip"`/`"song"` answer does the reverse -- a clip's scenes are only built
            # later, from the finished song's real section timing (design spec, "Сценарий").
            "project": {
                "type": ["object", "null"],
                "properties": {
                    "kind": {"type": "string", "enum": list(PROJECT_KINDS)},
                    "scenes": {
                        "type": ["array", "null"],
                        "items": {
                            "type": "object",
                            "properties": {
                                # A full, self-contained H3 prompt (docs/h3-prompt-system.md's own
                                # three-field format, flattened to the text the CLI's own
                                # `--prompt-file` already takes) -- not the structured
                                # instruction/description/soundscape/music object `prompt` above
                                # is, because a scene is written before its mode (`t2v` for the
                                # first scene, `i2v` off an automatic keyframe for every scene
                                # after it) is known; the pipeline prepends whichever `instruction`
                                # line applies once it actually submits the scene as a job.
                                "prompt": {"type": "string"},
                                # Review I1: the brief's own "5 to 10 seconds" (docs/h3-prompt-
                                # system.md, "Scenario mode") wasn't a schema bound before this --
                                # a model answering `duration: 60` passed validation silently and
                                # that number went straight into a GPU `generate` job with nothing
                                # downstream re-checking it.
                                "duration": {"type": "number", "minimum": 5, "maximum": 10},
                            },
                            "required": ["prompt", "duration"],
                            "additionalProperties": False,
                        },
                    },
                    "lyrics": {"type": ["string", "null"]},
                    "caption": {"type": ["string", "null"]},
                },
                "required": ["kind", "scenes", "lyrics", "caption"],
                "additionalProperties": False,
            },
        },
        "required": ["reply", "prompt"],
        "additionalProperties": False,
    },
}


# Task 2 ("Сюжет клипа"): the *later* step "Song mode" (in docs/h3-prompt-system.md, and in
# `PROMPT_SCHEMA["schema"]["properties"]["project"]` above) already promises -- "a clip's scenes
# are only built later, from the finished song's actual section timing." This is that step's own
# answer shape, and it is a *separate* top-level schema rather than a fourth key bolted onto
# `PROMPT_SCHEMA`: that schema already carries `prompt` (one clip) and `project` (a video script,
# or a song's lyrics+caption before it is even rendered) and does not need a third, unrelated
# shape mixed into the same object every ordinary chat turn is validated against.
SCENARIO_SCHEMA = {
    "name": "h3_clip_scenario",
    "schema": {
        "type": "object",
        "properties": {
            "reply": {"type": "string"},
            # Nullable-but-required, the same convention `prompt`/`project` use above in
            # `PROMPT_SCHEMA`: `null` while there is nothing to answer with yet (a clarifying
            # question), but the key itself always present in a valid turn.
            "scenario": {
                "type": ["object", "null"],
                "properties": {
                    "sections": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                # Names which part of the song this is (`verse`, `chorus`, ... or,
                                # for a raw Whisper transcript with no such tags, a short label of
                                # the modeler's own choosing) -- identifies the section, never
                                # itself part of a prompt.
                                "tag": {"type": "string"},
                                # Seconds into the track. Sections must cover the whole song with
                                # no gap and no overlap (docs/h3-prompt-system.md, "Clip scenario
                                # mode") -- enforced by the caller (coverage against the track's
                                # own `duration`), not by this schema, the same way `PROMPT_SCHEMA`
                                # leaves cross-field coverage checks to its own caller.
                                "start": {"type": "number"},
                                "end": {"type": "number"},
                                "scene": {
                                    "type": "object",
                                    "properties": {
                                        # A full, self-contained H3 prompt -- the same three-field
                                        # format `PROMPT_SCHEMA`'s own `project.scenes[].prompt`
                                        # uses for "Scenario mode", flattened to text.
                                        "prompt": {"type": "string"},
                                        # Same 5-10 s pipeline ceiling as `PROMPT_SCHEMA`'s own
                                        # `project.scenes[].duration` (review I1's reasoning
                                        # applies unchanged here): a clip is generated and
                                        # stitched per scene, and 10 s is the ceiling a single
                                        # clip is written to reach, independent of how long the
                                        # section's own `start`/`end` span actually runs.
                                        "duration": {"type": "number", "minimum": 5,
                                                    "maximum": 10},
                                    },
                                    "required": ["prompt", "duration"],
                                    "additionalProperties": False,
                                },
                            },
                            "required": ["tag", "start", "end", "scene"],
                            "additionalProperties": False,
                        },
                    },
                    # The visual bible for the whole clip, written once -- every `scene.prompt`
                    # above must still repeat it verbatim (docs/h3-prompt-system.md), the same
                    # "Scenario mode" identity-across-cuts rule `PROMPT_SCHEMA`'s own scenario
                    # already lives by; this field exists so the gate UI can show and edit it once,
                    # in one place, not to replace the copy inside every scene.
                    "style_block": {"type": "string"},
                },
                "required": ["sections", "style_block"],
                "additionalProperties": False,
            },
        },
        "required": ["reply", "scenario"],
        "additionalProperties": False,
    },
}


_SYSTEM_PROMPT_CACHE: str | None = None


def system_prompt() -> str:
    global _SYSTEM_PROMPT_CACHE
    if _SYSTEM_PROMPT_CACHE is None:
        path = Path(__file__).parent.parent / "docs" / "h3-prompt-system.md"
        _SYSTEM_PROMPT_CACHE = path.read_text(encoding="utf-8")
    return _SYSTEM_PROMPT_CACHE


def load_env(root) -> dict[str, str]:
    path = Path(root) / ".env"
    if not path.is_file():
        return {}
    env: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        env[name.strip()] = value.strip()
    return env


def load_providers(root) -> dict:
    path = Path(root) / "providers.json"
    if not path.is_file():
        return {"active": None, "providers": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    env = load_env(root)
    providers = {}
    for name, cfg in data.get("providers", {}).items():
        cfg = dict(cfg)
        if cfg.get("type") == "openai":
            wanted = cfg.get("api_key_env", "")
            if wanted and wanted not in env:
                cfg["available"], cfg["reason"] = False, f"нет токена {wanted}"
            else:
                cfg["available"], cfg["reason"] = True, None
        else:
            cfg["available"], cfg["reason"] = True, None
        providers[name] = cfg
    return {"active": data.get("active"), "providers": providers}


def local_ports(roster: dict) -> list[int]:
    """Every distinct port a `llama-local` provider in `roster` claims, in roster order.

    `roster` is the shape `load_providers` returns (`{"active", "providers"}`) -- callers pass
    the whole thing, not just `roster["providers"]`, so this reads the same value they already
    loaded rather than asking them to unwrap it.

    Every "queue outranks a resident LLM" check in this codebase used to look at `active` alone
    (`worker._llm_holds_gpu`, `web._llm_state`, `web._llm_unload`). A human can raise a
    `llama-local` provider that is **not** `active` -- the chat page's per-turn provider dropdown
    picks one directly, without ever touching `providers.json`'s `active` field -- so a roster
    with an active external provider and a resident local one on another (or, on the real deployed
    config, the *same*) port was invisible to all three checks. This is the single place that
    answers "which ports could possibly hold GPU memory right now", so the three checks agree by
    construction instead of by three separate people remembering the same rule.

    Ports repeat when two provider entries share one (the real roster does, both on 8080) --
    de-duplicated here so callers checking `port_alive` once per port don't pay for it twice.
    """
    ports: list[int] = []
    for cfg in roster.get("providers", {}).values():
        if cfg.get("type") != "llama-local":
            continue
        port = cfg.get("port", 0)
        if port and port not in ports:
            ports.append(port)
    return ports


def port_alive(port: int) -> bool:
    if not port:
        return False
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError):
        return False


class LlamaLocal:
    """Owns one llama-server process: spawn, health-poll, kill.

    The worker (Task 6) reuses `port_alive` directly to avoid re-spawning
    when the server is already up.
    """

    def __init__(self, name: str, cfg: dict, root):
        self.name, self.cfg, self.root = name, cfg, Path(root)

    def status(self) -> str:
        return "up" if port_alive(self.cfg.get("port", 0)) else "down"

    def ensure_up(self, timeout: float = 90.0, spawn=subprocess.Popen) -> None:
        if self.status() == "up":
            return
        log = self.root / "chat" / "llama.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        presets = str(Path(self.cfg["presets_ini"]).expanduser())
        cmd = [self.cfg["llama_server"],
               "--models-dir", str(Path(presets).parent),
               "--models-preset", presets,
               "--models-max", "1",
               "--host", "127.0.0.1", "--port", str(self.cfg["port"])]
        with open(log, "ab") as sink:
            spawn(cmd, stdout=sink, stderr=sink)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.status() == "up":
                return
            time.sleep(0.2)
        tail = ""
        if log.is_file():
            tail = "\n".join(log.read_text(encoding="utf-8", errors="replace").splitlines()[-10:])
        raise ProviderError("llama_did_not_start",
                            f"llama-server не ответил /health за {timeout:.0f} с\n{tail}")

    def shutdown(self) -> None:
        # pkill matches "llama-server" only (matches the binary name printed
        # in `ps`, not our helper's --llama_server flag value).
        subprocess.run(["pkill", "-f", "llama-server"], check=False)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and self.status() == "up":
            time.sleep(0.2)


def _base_url(cfg: dict) -> str:
    if cfg.get("type") == "openai":
        return cfg["base_url"].rstrip("/")
    return f"http://127.0.0.1:{cfg['port']}"


#: Default completion-token limit when a `providers.json` entry does not set its own `max_tokens`
#: (the wire key this rides under is a separate, provider-configurable choice -- see
#: `max_tokens_param` below). Found by the bug this constant fixes: caila.io's `claude-opus-5`
#: answers `finish_reason: "stop"` with a valid 43 KB, 19-section `SCENARIO_SCHEMA` reply -- the
#: pipeline's own full-song ceiling -- at ~20 000 completion tokens (measured directly, 226 s).
#: This sits above that measurement rather than on it.
#:
#: `llama-server`'s own default (no `max_tokens` sent at all) is generous enough that the bug this
#: fixes never showed up locally -- only a real external provider's much stingier default (4096
#: for the one that surfaced this) does. That asymmetry is why a *default* is needed at all rather
#: than always trusting whatever the provider does when the field is omitted.
DEFAULT_MAX_TOKENS = 24000


def _read_sse(r) -> tuple[str, str | None]:
    """Accumulate one OpenAI-style Server-Sent-Events stream into `(content, finish_reason)` --
    the exact tuple shape `_chat_turn`'s non-streaming `ask()` branch already returns, so the
    truncation/retry logic downstream (`_chat_turn` itself) does not know or care which wire shape
    produced it. `r` is an already-open response (the object `urllib.request.urlopen` hands back).

    Why this exists at all: caila.io -- the gateway in front of every external provider this file
    talks to -- drops a request that runs past some idle window between 4 and 7 minutes (measured:
    a full song-scenario call to `claude-opus-5` was cut with `RemoteDisconnected` at 7m16s; the
    same call with a shorter brief passed uncut at 226s). A single `SCENARIO_SCHEMA` reply
    (~20k completion tokens) is exactly the shape long enough to hit that wall. A stream keeps
    bytes moving on the wire the whole time it is being generated, which is what actually prevents
    the drop -- see `cfg.get("stream")` in `_chat_turn` for the config flag this is gated on.

    Wire format: each event is a `data: <json>` line, terminated by a blank line; a line starting
    with `:` is a comment. Both blank lines and comments are skipped, per the SSE spec every
    provider here follows. `data: [DONE]` ends the stream normally and is not itself JSON.

    Only `choices[0].delta.content` is collected. A reasoning model's `delta.reasoning` /
    `reasoning_content` / `thinking` is never read -- those keys simply are not `content`, so
    reasoning text can never be mistaken for the answer (the exact failure ai-writer 2.0's own
    ADR-065 warns about: "thinking models stream their reasoning in delta chunks", and a client
    that treats any chunk as the answer breaks on them). If a model spends its whole stream
    reasoning and answers with an empty `content`, that empty string comes back here exactly as an
    empty non-streaming `content` already does -- `_chat_turn` sends it through the same
    `chat_truncated` (if cut) or `bad_model_json` (if not) branches, honestly, not smuggled through
    as if reasoning were the reply.

    Two ways this refuses instead of guessing:
    - a `data:` line whose payload does not parse as JSON -- `bad_provider_reply`, the same code
      the non-streaming path uses for "a 200 whose body is not a completion": a chunk that breaks
      the wire protocol is exactly that, one frame later.
    - the stream ends -- server closes the connection, or a socket error surfaces while reading --
      before `[DONE]` ever arrived *and* no chunk ever carried a `finish_reason`: `chat_unreachable`.
      Named the same as an outright-refused connection on purpose: either way the provider did not
      finish talking, and a half-collected `content` is never returned as if it were whole -- the
      whole point of this fix is to hold a connection open long enough to finish, and an
      interrupted one failed at exactly that job.

    A stream that ends with a `finish_reason` already seen but no separate `[DONE]` frame is *not*
    treated as that same failure: `finish_reason` (`"stop"`, `"length"`, ...) is itself the
    model's own "I am done" signal, `[DONE]` is a protocol nicety layered on top of it, and some
    gateways close the socket right after the final chunk without ever sending it. Refusing that
    case would turn a provider quirk into a false `chat_unreachable` for a reply that in fact
    completed -- `finish_reason: "length"` still reaches `_chat_turn`'s own truncation check either
    way, so `chat_truncated` is not lost by tolerating this.
    """
    content_parts: list[str] = []
    finish_reason: str | None = None
    for raw_line in r:
        line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
        if not line or line.startswith(":"):
            continue
        if not line.startswith("data:"):
            continue
        data = line[len("data:"):].strip()
        if data == "[DONE]":
            return "".join(content_parts), finish_reason
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            raise ProviderError(
                "bad_provider_reply",
                f"провайдер прислал не-JSON фрагмент потока: {data[:400]}")
        choices = chunk.get("choices") if isinstance(chunk, dict) else None
        if not choices:
            continue
        choice = choices[0]
        delta = choice.get("delta") if isinstance(choice, dict) else None
        piece = delta.get("content") if isinstance(delta, dict) else None
        if piece:
            content_parts.append(piece)
        fr = choice.get("finish_reason") if isinstance(choice, dict) else None
        if fr:
            finish_reason = fr
    # The loop ran out (the socket hit EOF) without ever seeing `[DONE]`. If a `finish_reason`
    # already arrived, the model itself signalled the end -- `[DONE]` was only a missing formality,
    # not a cut reply -- so this is a normal completion, `finish_reason` ("length" included) intact
    # for `_chat_turn`'s own truncation check.
    if finish_reason is not None:
        return "".join(content_parts), finish_reason
    # No `finish_reason` ever arrived either: the connection closed, or was cut, with no signal at
    # all that generation ended. Reported the same way an unreachable provider is: whatever partial
    # `content` was collected above is discarded rather than returned as if it were complete.
    raise ProviderError(
        "chat_unreachable",
        "поток оборвался раньше [DONE] -- провайдер закрыл соединение посреди ответа")


def _chat_turn(cfg: dict, env: dict, messages: list[dict], schema: dict,
               retry_reminder: str) -> dict:
    """One turn of the OpenAI chat protocol, response shaped by `schema`.

    The mechanics shared by every schema this module knows how to ask for -- `chat` (PROMPT_SCHEMA)
    and `chat_scenario` (SCENARIO_SCHEMA, Task 2, "Сюжет клипа") are both a thin wrapper around
    this: only the schema and the retry reminder's own wording (naming that schema's own top-level
    keys back to the model) differ between them.

    Both provider kinds speak the same wire format; only the base URL and
    the optional bearer token differ. A model that fails to hold the JSON
    shape gets exactly one retry with a system reminder appended, then a
    named ProviderError carrying the raw text for diagnosis.

    Four named failures leave here, and they are four because the page says four different
    things: `chat_unreachable` (nobody answered), `bad_provider_reply` (a 200 that is not a
    completion -- the provider's own error envelope), `chat_truncated` (the provider cut the
    reply short at its own output-token limit before the schema was finished) and `bad_model_json`
    (a completion whose text is not the schema, and was not cut short). Only the last is worth a
    retry: the other three are not the model failing to phrase an answer, they are there being no
    answer to phrase -- and for `chat_truncated` specifically, retrying with the same limit would
    hit the same wall again, so it is not retried at all (see below).

    `cfg["stream"]` (default `False`) switches the wire shape from one plain JSON body to Server-
    Sent Events, parsed by `_read_sse` into the exact same `(content, finish_reason)` tuple the
    plain path produces -- everything below this point (truncation check, retry, the four named
    failures) runs unchanged and does not know which shape produced its input.
    """
    max_tokens = cfg.get("max_tokens")
    if max_tokens is None:
        max_tokens = DEFAULT_MAX_TOKENS
        # `llama-local`'s own `ctx` is llama.cpp's `n_ctx` -- prompt *and* completion sharing one
        # budget, unlike an external provider's separate `max_tokens`. Defaulting to a completion
        # budget sized for the measurement above (~20k) on a `ctx` that does not have 20k tokens
        # to spare after the prompt would turn this fix into the same failure with an extra step.
        # Only the *implicit* default is capped this way: a `max_tokens` a human actually wrote
        # into `providers.json` is their own choice and is sent unchanged, `ctx` included -- this
        # `if` is only reachable when they wrote none.
        ctx = cfg.get("ctx")
        if ctx:
            max_tokens = min(max_tokens, max(ctx // 2, 1024))
    # Which JSON key the limit above rides on. Never both, and never guessed from the model name:
    # caila.io's Anthropic routes (`just-ai/anthropic-claude/...`) silently *ignore*
    # `max_completion_tokens` -- the whole budget still goes to the provider's own default -- and
    # need `max_tokens`; OpenAI's own reasoning models (`o1`-`o4`, `gpt-5*`) do the opposite and
    # *reject* `max_tokens` outright, needing `max_completion_tokens`. `"max_tokens"` is the
    # default because it is what every provider this file currently talks to except one reasoning
    # model needs -- llama-server, an ordinary (non-reasoning) `openai`-typed provider, and
    # caila's Anthropic routes all take it; the one exception (a reasoning model behind caila)
    # sets `max_tokens_param: "max_completion_tokens"` in its own `providers.json` entry.
    token_limit_key = cfg.get("max_tokens_param", "max_tokens")
    body = {"model": cfg.get("model", cfg.get("preset", "default")),
            "messages": messages,
            token_limit_key: max_tokens,
            "response_format": {"type": "json_schema", "json_schema": schema}}
    # `send_temperature: false` in a provider's own entry leaves `temperature` out of the body
    # entirely. Default `true` (unchanged behaviour): every provider this file currently talks to
    # sends an explicit `temperature` in `providers.json` and today's live caila.io call still
    # answered `claude-opus-5` with `temperature: 0.7` accepted -- but caila.io is on record
    # (its own outage, "инцидент #273") rejecting `temperature` on *some* of its routes, and an
    # OpenAI reasoning model is on record rejecting it outright. Rather than guess which of this
    # roster's entries is next, the escape hatch is a config flag: a provider that starts refusing
    # `temperature` gets `send_temperature: false` in its own entry, no code change.
    send_temperature = cfg.get("send_temperature", True)
    if send_temperature:
        body["temperature"] = cfg.get("temperature", 0.7)
    # `stream: true` in a provider's own entry asks for Server-Sent Events instead of one plain
    # JSON body -- the workaround for the caila.io idle-drop measured directly against
    # `claude-opus-5` (`RemoteDisconnected` at 7m16s on a full song-scenario call, the same call
    # uncut at 226s with a shorter brief): a stream keeps bytes moving on the wire for the whole
    # ~20k-completion-token reply, which is what actually prevents the drop -- switching model or
    # schema does not. See `_read_sse` for how the chunks are parsed back into one reply.
    #
    # Default `False`, and omitted from the body entirely rather than sent as `false`, the same
    # convention `send_temperature`'s own flag uses: today's plain request/response path already
    # works against both `llama-local` (no gateway sits in front of it -- this failure has never
    # shown up there) and every external provider currently in the roster, and flipping the wire
    # shape globally risks breaking a codepath that has nothing to fix. This is an escape hatch a
    # provider opts into in its own `providers.json` entry, not a default behaviour change.
    stream = cfg.get("stream", False)
    if stream:
        body["stream"] = True
    headers = {"Content-Type": "application/json"}
    key_env = cfg.get("api_key_env")
    if cfg.get("type") == "openai" and key_env:
        headers["Authorization"] = f"Bearer {env.get(key_env, '')}"

    def ask(msgs):
        """Returns `(content, finish_reason)` -- `finish_reason` may be `None`, not every provider
        sends one, and its absence is not itself a failure."""
        req = urllib.request.Request(_base_url(cfg) + "/v1/chat/completions",
                                     data=json.dumps({**body, "messages": msgs}).encode(),
                                     headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                if stream:
                    # `_read_sse` reads `r` to completion (or raises) itself -- its own
                    # `chat_unreachable`/`bad_provider_reply` for a dropped or malformed stream
                    # must reach the caller unchanged, not get relabelled by the `except` below,
                    # which is why it returns straight out of this `try` rather than assigning
                    # into a variable the `except` could shadow.
                    return _read_sse(r)
                payload = json.loads(r.read())
        # `http.client.HTTPException` (`IncompleteRead` among others) is not an `OSError` -- a
        # stream cut mid-response by a socket-level failure surfaces through it, not through
        # `URLError`/`OSError`, and without it here that failure would escape as a raw, unnamed
        # exception instead of the same honest `chat_unreachable` an outright-refused connection
        # already gets.
        except (urllib.error.URLError, OSError, http.client.HTTPException) as err:
            # Connection refused (server not up / crashed), timeout, or any
            # other transport failure -- never leak the raw urllib exception
            # (or headers, which may carry the bearer token) to the caller.
            raise ProviderError("chat_unreachable", f"провайдер недоступен: {err}")
        # A 200 does not mean the body is a completion. OpenRouter answers 200 with
        # `{"error": {"message": ...}}` when *its* upstream fails, and a proxy in front of any
        # provider can answer 200 with something else entirely. Walking that with plain
        # subscripting raised `KeyError`/`TypeError`, which is not a `ProviderError` -- so the
        # server's `except provider.ProviderError` missed it and the page was told 500 «сервер
        # споткнулся» for a failure that never was this server's. The envelope is checked here,
        # once, where the bytes are parsed.
        try:
            choice = payload["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise ProviderError(
                "bad_provider_reply",
                f"провайдер ответил 200, но не ходом: "
                f"{json.dumps(payload, ensure_ascii=False)[:400]}")
        return content, (choice.get("finish_reason") if isinstance(choice, dict) else None)

    def _truncated() -> ProviderError:
        # Named separately from `bad_model_json`: the model did not break the schema, it was
        # stopped before it could finish speaking. Conflating the two used to show a person «модель
        # не удержала формат: » with an empty tail for a model that never got the chance to hold
        # any format at all -- a dead end pointing at the wrong culprit.
        return ProviderError(
            "chat_truncated",
            f"ответ обрезан лимитом вывода ({token_limit_key}={max_tokens}) раньше, чем модель "
            f"закончила -- поднимите `{token_limit_key}` у этого провайдера в providers.json")

    raw, finish_reason = ask(messages)
    if finish_reason == "length":
        raise _truncated()
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        reminder = {"role": "system", "content": retry_reminder}
        raw2, finish_reason2 = ask([reminder, *messages])
        if finish_reason2 == "length":
            raise _truncated()
        try:
            return json.loads(raw2)
        except (json.JSONDecodeError, TypeError):
            raise ProviderError("bad_model_json", f"модель не удержала формат: {raw2[:400]}")


def chat(cfg: dict, env: dict, messages: list[dict]) -> dict:
    """One turn of the OpenAI chat protocol, response shaped by PROMPT_SCHEMA.

    See `_chat_turn` for the shared mechanics (retry, the four named failures) this and
    `chat_scenario` both build on.
    """
    return _chat_turn(cfg, env, messages, PROMPT_SCHEMA,
                      "Ответ строго одним JSON-объектом по схеме "
                      "{reply: string, prompt: object|null}. Без другого текста.")


def chat_scenario(cfg: dict, env: dict, messages: list[dict]) -> dict:
    """One turn of the OpenAI chat protocol, response shaped by SCENARIO_SCHEMA -- the "Clip
    scenario mode" call (Task 2, "Сюжет клипа"): turning a finished song's real section timing (or
    a raw Whisper transcript, when there was no reference lyrics) into per-section H3 video
    prompts. Same wire protocol, same provider roster, same `ensure_up`/error/retry mechanics as
    `chat` -- shared through `_chat_turn` rather than duplicated -- only the schema and the retry
    reminder's own wording differ. Callers build `messages` themselves (`system_prompt()` plus the
    scenario's own context: lyrics or raw_segments, caption, duration), the same way the chat route
    already builds `chat`'s own messages.
    """
    return _chat_turn(cfg, env, messages, SCENARIO_SCHEMA,
                      "Ответ строго одним JSON-объектом по схеме "
                      "{reply: string, scenario: object|null}. Без другого текста.")


def test_provider(cfg: dict, env: dict, timeout: float = 5.0) -> dict:
    """A cheap connectivity probe for one provider entry -- Task 2 ("выбор провайдера для
    сценария"), the idea taken from ai-writer 2.0's `ProviderService.test_connection` (`GET
    {base_url}/models`) but scaled to what this module actually needs: no detected-capabilities
    job, just "does this provider answer at all", asked *before* a scenario turn, not after it
    times out.

    Returns `{"ok": bool, "reachable": bool, "detail": str, ...}` -- never raises, so a route can
    hand the dict straight back as the response body without its own try/except. `ok` is this
    module's verdict on whether the provider is currently *usable*; `reachable` is a narrower
    "did a socket answer", kept separate because `llama-local` down is `ok=True` (an ordinary
    state -- `ensure_up` will raise it at generation time) while `reachable=False` for it, and an
    `openai` provider that answers with the wrong shape is `reachable=True` but `ok=False`.

    `type: "openai"` -- `GET {base_url}/v1/models`, the same bearer header `_chat_turn` sends,
    `timeout` seconds (a handful, not `_chat_turn`'s own 600s: a probe is worth nothing if it can
    itself hang for a minute). **Not `{base_url}/models`** -- `_base_url` (defined earlier in
    this module) does not add or strip `/v1` at all, for either provider kind: for `type:
    "openai"` it is only `cfg["base_url"].rstrip("/")`. `_chat_turn` is the one that appends
    `/v1/chat/completions` itself, and this appends `/v1/models` the same way -- so **`base_url`
    in `providers.json` must be given without a trailing `/v1`**. This matters beyond a typo:
    OpenRouter's own docs quote a base URL that already ends in `/v1`, and copying that verbatim
    here would silently double it into `/v1/v1/models` (and `/v1/v1/chat/completions` for
    `chat`/`chat_scenario` too) -- a 404 on every provider that follows that convention, chat
    included, not just this probe.

    Success returns however many model ids the response named (`models`, capped at
    20 so a provider with hundreds does not bloat the response); failure -- no token, no answer,
    or a 200 that is not a models list -- is a plain, honest `detail`, never the raw exception
    (which can carry a URL with a query string) and never the token itself, which never leaves
    this function to begin with -- it goes out in a header, not a returned value.

    `type: "llama-local"` -- `port_alive` only, no request built or sent. A port that is not up
    is not a failure this function reports as one (`ok=True`): the model raises on its own
    schedule (`ensure_up`, at generation time), and a probe screaming red for a state that is
    completely ordinary would train a person to ignore the red the one time it means something.
    """
    if cfg.get("type") == "llama-local":
        alive = port_alive(cfg.get("port", 0))
        return {
            "ok": True,
            "reachable": alive,
            "detail": ("llama-server отвечает" if alive
                      else "порт не поднят — поднимется при генерации"),
        }

    headers = {}
    key_env = cfg.get("api_key_env")
    if key_env:
        headers["Authorization"] = f"Bearer {env.get(key_env, '')}"

    req = urllib.request.Request(_base_url(cfg) + "/v1/models", headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except (urllib.error.URLError, OSError) as err:
        # Same care `_chat_turn`'s own `chat_unreachable` takes: the raw exception (and any
        # header it might echo back) never reaches the caller, only a plain sentence.
        return {"ok": False, "reachable": False, "detail": f"провайдер недоступен: {err}"}

    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {"ok": False, "reachable": True,
               "detail": "провайдер ответил, но не JSON"}

    models = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        # The raw body never rides `detail` (M3, review round after the first landing): the
        # one branch that gets this far already has a 200 from *something* at `base_url`, and
        # that something is not necessarily the provider itself -- a misconfigured proxy in
        # front of it can echo request headers (the bearer token among them) back in an error
        # body, and this probe's whole point is to be safe to click without a second thought.
        return {"ok": False, "reachable": True,
               "detail": "провайдер ответил 200, но не списком моделей"}
    names = [m.get("id") for m in models if isinstance(m, dict) and m.get("id")]
    count = len(models)
    return {
        "ok": True,
        "reachable": True,
        "detail": f"{count} {'модель' if count == 1 else 'моделей'}"
                 + (f": {', '.join(names[:20])}" if names else ""),
        "models": names[:20],
    }
