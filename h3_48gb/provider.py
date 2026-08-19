"""LLM providers for the chat prompt editor.

Two kinds speak one protocol (OpenAI /v1/chat/completions): `llama-local`
also owns the llama-server process lifecycle, `openai` only needs a URL and
a token. Tokens never live in providers.json -- only the *name* of an .env
variable does, so the roster can be shown to the page verbatim.
"""
from __future__ import annotations

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

    Three named failures leave here, and they are three because the page says
    three different things: `chat_unreachable` (nobody answered),
    `bad_provider_reply` (a 200 that is not a completion -- the provider's own
    error envelope) and `bad_model_json` (a completion whose text is not the
    schema). Only the last is worth a retry: the other two are not the model
    failing to phrase an answer, they are there being no answer to phrase.
    """
    body = {"model": cfg.get("model", cfg.get("preset", "default")),
            "messages": messages,
            "temperature": cfg.get("temperature", 0.7),
            "response_format": {"type": "json_schema", "json_schema": schema}}
    headers = {"Content-Type": "application/json"}
    key_env = cfg.get("api_key_env")
    if cfg.get("type") == "openai" and key_env:
        headers["Authorization"] = f"Bearer {env.get(key_env, '')}"

    def ask(msgs):
        req = urllib.request.Request(_base_url(cfg) + "/v1/chat/completions",
                                     data=json.dumps({**body, "messages": msgs}).encode(),
                                     headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                payload = json.loads(r.read())
        except (urllib.error.URLError, OSError) as err:
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
            return payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise ProviderError(
                "bad_provider_reply",
                f"провайдер ответил 200, но не ходом: "
                f"{json.dumps(payload, ensure_ascii=False)[:400]}")

    raw = ask(messages)
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        reminder = {"role": "system", "content": retry_reminder}
        raw2 = ask([reminder, *messages])
        try:
            return json.loads(raw2)
        except (json.JSONDecodeError, TypeError):
            raise ProviderError("bad_model_json", f"модель не удержала формат: {raw2[:400]}")


def chat(cfg: dict, env: dict, messages: list[dict]) -> dict:
    """One turn of the OpenAI chat protocol, response shaped by PROMPT_SCHEMA.

    See `_chat_turn` for the shared mechanics (retry, the three named failures) this and
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
