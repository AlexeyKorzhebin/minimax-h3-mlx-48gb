"""Провайдеры LLM: конфиг, .env, жизненный цикл llama-server, ход чата.

llama-server здесь всегда фальшивый: настоящий грузит 30 ГБ. Мок — обычный
http.server в потоке, отвечающий на /health и /v1/chat/completions; он живёт в
`tests/_fake_llama.py`, потому что тем же моком пользуются маршруты чата
(`tests/test_chat_web.py`).
"""
import json
import textwrap

import pytest

from _fake_llama import _TURN, _FakeLlama
from h3_48gb import provider


def _write(root, name, text):
    path = root / name
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


def test_env_file_is_parsed_line_by_line_and_absence_is_empty(tmp_path):
    assert provider.load_env(tmp_path) == {}
    _write(tmp_path, ".env", """\
        # комментарий
        OPENROUTER_API_KEY=sk-or-abc

        EMPTY_TAIL=
    """)
    env = provider.load_env(tmp_path)
    assert env["OPENROUTER_API_KEY"] == "sk-or-abc"
    assert env["EMPTY_TAIL"] == ""
    assert "# комментарий" not in env


def test_missing_providers_file_is_an_empty_roster(tmp_path):
    assert provider.load_providers(tmp_path) == {"active": None, "providers": {}}


def test_external_provider_without_its_token_is_visible_but_unavailable(tmp_path):
    (tmp_path / "providers.json").write_text(json.dumps({
        "active": "qwen-local",
        "providers": {
            "qwen-local": {"type": "llama-local", "llama_server": "/opt/homebrew/bin/llama-server",
                           "presets_ini": "~/models/presets.ini", "preset": "qwen", "port": 18080,
                           "ctx": 49152, "resident_gb": 31},
            "openrouter": {"type": "openai", "base_url": "https://example.invalid/v1",
                           "model": "m", "api_key_env": "OPENROUTER_API_KEY"},
        },
    }), encoding="utf-8")
    roster = provider.load_providers(tmp_path)
    assert roster["active"] == "qwen-local"
    assert roster["providers"]["qwen-local"]["available"] is True
    ext = roster["providers"]["openrouter"]
    assert ext["available"] is False
    assert "OPENROUTER_API_KEY" in ext["reason"]
    # ключ появился — провайдер ожил
    _write(tmp_path, ".env", "OPENROUTER_API_KEY=sk-x\n")
    assert provider.load_providers(tmp_path)["providers"]["openrouter"]["available"] is True


def test_the_loaded_roster_never_carries_the_secret_itself(tmp_path):
    _write(tmp_path, ".env", "OPENROUTER_API_KEY=sk-very-secret\n")
    (tmp_path / "providers.json").write_text(json.dumps({
        "active": "openrouter",
        "providers": {"openrouter": {"type": "openai", "base_url": "https://example.invalid/v1",
                                     "model": "m", "api_key_env": "OPENROUTER_API_KEY"}},
    }), encoding="utf-8")
    assert "sk-very-secret" not in json.dumps(provider.load_providers(tmp_path))


def _llama_cfg(port: int) -> dict:
    return {"type": "llama-local", "llama_server": "/usr/bin/true",
            "presets_ini": "/tmp/presets.ini", "preset": "qwen", "port": port,
            "ctx": 4096, "resident_gb": 31}


def test_status_reflects_health_endpoint(tmp_path):
    fake = _FakeLlama()
    try:
        assert provider.LlamaLocal("q", _llama_cfg(fake.port), tmp_path).status() == "up"
    finally:
        fake.close()
    assert provider.LlamaLocal("q", _llama_cfg(fake.port), tmp_path).status() == "down"


def test_ensure_up_spawns_llama_with_the_preset_flags(tmp_path):
    """Не поднимаем настоящего: spawn подменён, health отвечает мок, а тест
    проверяет ровно командную строку — то, что сломается молча."""
    fake = _FakeLlama()
    spawned: list[list[str]] = []

    def spawn(cmd, **kw):
        spawned.append(cmd)
        class P: pid = 1
        return P()

    try:
        lam = provider.LlamaLocal("q", _llama_cfg(fake.port), tmp_path)
        lam.ensure_up(timeout=5, spawn=spawn)   # health уже 200 -> spawn не нужен
        assert spawned == []
    finally:
        fake.close()

    lam = provider.LlamaLocal("q", _llama_cfg(0), tmp_path)  # порт 0 всегда down
    with pytest.raises(provider.ProviderError) as err:
        lam.ensure_up(timeout=0.3, spawn=spawn)
    assert err.value.code == "llama_did_not_start"
    (cmd,) = spawned
    assert cmd[0] == "/usr/bin/true"
    assert "--models-preset" in cmd and "/tmp/presets.ini" in cmd
    assert "--models-max" in cmd and "1" in cmd[cmd.index("--models-max") + 1]


def test_chat_sends_schema_and_returns_parsed_turn(tmp_path):
    fake = _FakeLlama(chat_payload=_TURN)
    try:
        turn = provider.chat(_llama_cfg(fake.port), {}, [{"role": "user", "content": "мрачнее"}])
    finally:
        fake.close()
    assert turn["reply"] == "Сделал мрачнее."
    assert turn["prompt"]["non_diegetic_music"] == "N/A"
    (req,) = fake.requests
    assert req["path"] == "/v1/chat/completions"
    assert req["body"]["response_format"]["json_schema"] == provider.PROMPT_SCHEMA


def test_invalid_model_json_gets_one_retry_then_a_named_error(tmp_path):
    fake = _FakeLlama(chat_payload={"choices": [{"message": {"content": "не json"}}]})
    try:
        with pytest.raises(provider.ProviderError) as err:
            provider.chat(_llama_cfg(fake.port), {}, [{"role": "user", "content": "x"}])
        assert err.value.code == "bad_model_json"
        assert len(fake.requests) == 2, "должен быть ровно один повтор"
    finally:
        fake.close()


def test_a_two_hundred_carrying_a_providers_own_error_is_a_named_refusal(tmp_path):
    """OpenRouter answers 200 with `{"error": {...}}` when *its* upstream fails.

    That body is valid JSON and has no `choices`, so the plain
    `payload["choices"][0]["message"]["content"]` walked into a `KeyError` — which is not a
    `ProviderError`, so `_locked_turn`'s `except provider.ProviderError` never saw it and the page
    got a 500 «сервер споткнулся» for a failure that is entirely the provider's. The whole point
    of the code contract is that the page can tell «модель/провайдер подвёл» from «сервер сломан»,
    and 500 says the wrong one of the two.
    """
    for payload in ({"error": {"message": "x"}},           # OpenRouter's upstream-failure shape
                    {"choices": []},                        # 200, no choice at all
                    {"choices": [{"message": {}}]},         # a choice with no content
                    {"choices": "нет"}):                    # `choices` that is not a list
        fake = _FakeLlama(chat_payload=payload)
        try:
            with pytest.raises(provider.ProviderError) as err:
                provider.chat(_llama_cfg(fake.port), {}, [{"role": "user", "content": "x"}])
        finally:
            fake.close()
        assert err.value.code == "bad_provider_reply", payload
        assert len(fake.requests) == 1, "ответ не по форме — не повод переспрашивать"

    # The body reaches the message so a person can see what the provider actually said, and is
    # cut, so a megabyte of HTML from a captive portal does not become the error message.
    fake = _FakeLlama(chat_payload={"error": {"message": "ы" * 5000}})
    try:
        with pytest.raises(provider.ProviderError) as err:
            provider.chat(_llama_cfg(fake.port), {}, [{"role": "user", "content": "x"}])
    finally:
        fake.close()
    assert "ыыы" in str(err.value)
    assert len(str(err.value)) < 600, str(err.value)[:100]


def test_external_provider_authorises_with_its_env_token(tmp_path):
    fake = _FakeLlama(chat_payload=_TURN)
    cfg = {"type": "openai", "base_url": f"http://127.0.0.1:{fake.port}",
           "model": "m", "api_key_env": "K"}
    try:
        provider.chat(cfg, {"K": "sk-t"}, [{"role": "user", "content": "x"}])
        assert fake.requests[0]["headers"].get("Authorization") == "Bearer sk-t"
    finally:
        fake.close()


def test_chat_with_no_provider_listening_raises_a_named_error(tmp_path):
    fake = _FakeLlama()
    port = fake.port
    fake.close()  # никто больше не слушает этот порт -> connection refused
    with pytest.raises(provider.ProviderError) as err:
        provider.chat(_llama_cfg(port), {}, [{"role": "user", "content": "x"}])
    assert err.value.code == "chat_unreachable"


def test_system_prompt_carries_the_format_and_the_preservation_rule():
    text = provider.system_prompt()
    for anchor in ("integrated_multimodal_description", "overall_soundscape",
                   "non_diegetic_music", "[Shot 1]", "<scenetrans>", "Arc Shot",
                   "preserve", "JSON",
                   # T4: `mode: flf` (FL2VA) is documented, not left for the model to invent --
                   # this is the literal instruction line from the upstream guide
                   # (VIDEO_PROMPT_WRITING_GUIDE_base_en.md), verbatim except for its N/S.SS
                   # placeholders.
                   "How the reference pictures align with the target video",
                   # A4: the doc has to tell the model to hand back a slug, and show it the
                   # shape the example is supposed to take.
                   "slug", "cat-italian-noon"):
        assert anchor in text, anchor


# -- A4: slug -----------------------------------------------------------------------------------


def test_prompt_schema_carries_an_optional_slug_field_outside_required():
    """The schema is what a provider validates a completion against, so a `slug` this server can
    read has to be *in* it -- and outside `required`, or every turn the model answered before A4
    (none of them carrying the key at all) would stop being a valid answer to this same schema.
    """
    schema = provider.PROMPT_SCHEMA["schema"]
    assert schema["properties"]["slug"]["type"] == ["string", "null"]
    assert "slug" not in schema["required"]


def test_chat_returns_the_slug_the_model_answered_with(tmp_path):
    payload = {"choices": [{"message": {"content": json.dumps(
        {"reply": "ок", "prompt": None, "slug": "cat-italian-noon"})}}]}
    fake = _FakeLlama(chat_payload=payload)
    try:
        turn = provider.chat(_llama_cfg(fake.port), {}, [{"role": "user", "content": "x"}])
    finally:
        fake.close()
    assert turn["slug"] == "cat-italian-noon"


def test_a_turn_with_no_slug_at_all_is_still_a_valid_parse(tmp_path):
    """`_TURN` is the shape every other provider test answers with, and it carries no `slug` key
    -- exactly what a pre-A4 saved turn, or a provider that never learned the new field, looks
    like. `chat` must not choke on its absence.
    """
    fake = _FakeLlama(chat_payload=_TURN)
    try:
        turn = provider.chat(_llama_cfg(fake.port), {}, [{"role": "user", "content": "x"}])
    finally:
        fake.close()
    assert turn.get("slug") is None
