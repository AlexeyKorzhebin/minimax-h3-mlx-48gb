"""The studio's door to an LLM (spec §9.1, §8.4). `Llm` is what ops and the director depend on;
`ProviderLlm` is the real one over `provider.chat_with_schema`, configured by
`<outdir>/studio/providers.json` + `.env` (the same roster format as the old panel)."""
from __future__ import annotations

import fcntl
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol

import jsonschema

from h3_48gb import provider
from h3_48gb.studio.errors import StudioError


class Llm(Protocol):
    label: str

    def json_turn(self, messages: list[dict], schema: dict, retry_reminder: str) -> dict: ...


def validate_reply(reply, schema: dict) -> None:
    """Check a model's reply against the studio's own schema, whatever the provider's mode.

    `provider._chat_turn` checks the schema itself only for `json_object` routes and trusts the
    server for `json_schema` (tests/test_provider.py pins that), so `{}` can arrive here. The
    refusal names the place, never the value: the reply is the model's text."""
    error = jsonschema.exceptions.best_match(
        jsonschema.Draft202012Validator(schema["schema"]).iter_errors(reply))
    if error is not None:
        path = "/" + "/".join(str(p) for p in error.absolute_path)
        raise StudioError(502, "llm_failed", "Модель ответила не по схеме",
                          {"schema": schema["name"], "path": path, "rule": error.validator})


@contextmanager
def gpu_lock(path: Path, *, enabled: bool, timeout_s: float):
    """§8.4 `local-mac`: an LLM that shares the GPU waits for the same flock the worker holds
    while rendering — at most `timeout_s`, then 409 `gpu_busy` instead of hanging the request."""
    if not enabled:
        yield
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise StudioError(409, "gpu_busy",
                                      "Сценарист ждёт видеокарту: её занял рендер",
                                      {"lock": str(path)}) from None
                time.sleep(0.1)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


#: What the owner reads when a provider call fails. Only these fixed texts leave the studio: the
#: provider's own message can carry the model's raw reply (`bad_model_json` quotes 400 characters
#: of it unredacted, provider.py) or an echoed token, and it goes into API bodies and reports.
SAFE_PROVIDER_MESSAGES = {
    "chat_unreachable": "Сценарист недоступен: провайдер не ответил",
    "bad_provider_reply": "Сценарист недоступен: провайдер вернул ошибку",
    "chat_truncated": "Ответ сценариста обрезан лимитом вывода провайдера (max_tokens)",
    "bad_model_json": "Модель ответила не по схеме",
    "bad_provider_config": "Неверная настройка провайдера в studio/providers.json",
}


_ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]*")
_PROVIDER_NAME = re.compile(r"[a-z][a-z0-9_]*(-[a-z0-9_]+)*")
_MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./:@+-]*")


def _shown(value, pattern: re.Pattern, *, limit: int) -> str | None:
    """A value that came from providers.json or the caller may go into a message or `detail`
    only when it is shaped like a name. The file is the owner's and a key pasted into the wrong
    field (`api_key_env`, a provider name, a model) is a secret: whatever is not name-shaped is
    shown as "invalid", never echoed. `None` stays `None` (nothing was configured)."""
    if value is None:
        return None
    if (isinstance(value, str) and len(value) <= limit and pattern.fullmatch(value)
            and not value.startswith("sk-")):
        return value
    return "invalid"


def _provider_shown(name):
    return _shown(name, _PROVIDER_NAME, limit=32)


class ProviderLlm:
    def __init__(self, home: Path, lock_path: Path, *, provider_name: str | None = None,
                 lock_timeout_s: float = 5.0):
        self.home = home
        self.lock_path = lock_path
        self.provider_name = provider_name
        self.lock_timeout_s = lock_timeout_s

    def _config(self) -> tuple[str, dict]:
        try:
            roster = provider.load_providers(self.home)
        except (ValueError, OSError, AttributeError, TypeError):
            # The file's own text (a JSON error quotes a slice of it) stays out of the refusal.
            raise StudioError(503, "llm_not_configured",
                              "Сценарист не настроен: studio/providers.json не читается",
                              {"provider": _provider_shown(self.provider_name)}) from None
        name = self.provider_name or roster["active"]
        cfg = roster["providers"].get(name) if isinstance(name, str) and name else None
        if cfg is None:
            raise StudioError(503, "llm_not_configured",
                              "Сценарист не настроен: нет провайдера в studio/providers.json",
                              {"provider": _provider_shown(name)})
        if not cfg["available"]:
            # `cfg["reason"]` is built by `load_providers` around the raw `api_key_env` text;
            # the owner may have pasted the key itself there. Fixed text, vetted name in detail.
            raise StudioError(503, "llm_not_configured",
                              "Сценарист не настроен: нет токена в .env",
                              {"provider": _provider_shown(name),
                               "api_key_env": _shown(cfg.get("api_key_env"), _ENV_NAME,
                                                     limit=64)})
        needed = "base_url" if cfg.get("type") == "openai" else "port"
        if not cfg.get(needed):
            raise StudioError(503, "llm_not_configured",
                              f"Сценарист не настроен: у провайдера нет {needed}",
                              {"provider": _provider_shown(name)})
        return name, cfg

    @property
    def label(self) -> str:
        try:
            name, cfg = self._config()
        except StudioError:
            return "none"
        model = _shown(cfg.get("model", cfg.get("preset", "")) or "", _MODEL_NAME, limit=80)
        return f"{_provider_shown(name)}:{model}"

    def json_turn(self, messages: list[dict], schema: dict, retry_reminder: str) -> dict:
        name, cfg = self._config()
        with gpu_lock(self.lock_path, enabled=provider.shares_gpu(cfg) is True,
                      timeout_s=self.lock_timeout_s):
            try:
                return provider.chat_with_schema(cfg, provider.load_env(self.home), messages,
                                                 schema, retry_reminder)
            except provider.ProviderError as err:
                code = err.code      # only the code leaves the handler: not `err`, not its text
            # Raised outside the `except`: inside it, `__context__` would carry the ProviderError
            # (its message can quote the model's raw reply or an echoed token) into any traceback.
            raise StudioError(502, "llm_failed",
                              SAFE_PROVIDER_MESSAGES.get(code, "Сценарист не ответил"),
                              {"provider": _provider_shown(name), "provider_code": code})
