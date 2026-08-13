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
        },
        "required": ["reply", "prompt"],
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


def chat(cfg: dict, env: dict, messages: list[dict]) -> dict:
    """One turn of the OpenAI chat protocol, response shaped by PROMPT_SCHEMA.

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
            "response_format": {"type": "json_schema", "json_schema": PROMPT_SCHEMA}}
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
        reminder = {"role": "system",
                    "content": "Ответ строго одним JSON-объектом по схеме "
                               "{reply: string, prompt: object|null}. Без другого текста."}
        raw2 = ask([reminder, *messages])
        try:
            return json.loads(raw2)
        except (json.JSONDecodeError, TypeError):
            raise ProviderError("bad_model_json", f"модель не удержала формат: {raw2[:400]}")
