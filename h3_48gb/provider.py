"""LLM providers for the chat prompt editor.

Two kinds speak one protocol (OpenAI /v1/chat/completions): `llama-local`
also owns the llama-server process lifecycle, `openai` only needs a URL and
a token. Tokens never live in providers.json -- only the *name* of an .env
variable does, so the roster can be shown to the page verbatim.
"""
from __future__ import annotations

import json
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
        },
        "required": ["reply", "prompt"],
        "additionalProperties": False,
    },
}


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
