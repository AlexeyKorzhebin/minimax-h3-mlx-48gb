"""Провайдеры LLM: конфиг, .env, жизненный цикл llama-server, ход чата.

llama-server здесь всегда фальшивый: настоящий грузит 30 ГБ. Мок — обычный
http.server в потоке, отвечающий на /health и /v1/chat/completions; он живёт в
`tests/_fake_llama.py`, потому что тем же моком пользуются маршруты чата
(`tests/test_chat_web.py`).
"""
import http.client
import json
import re
import textwrap
from unittest.mock import patch

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


# -- fix round: `max_tokens` never sent, and a truncated reply misreported as `bad_model_json` --
#
# Found live against caila.io's `claude-opus-5`: with no `max_tokens` in the request, the
# provider's own default (4096) is far too small for a full scenario reply, the model spends the
# whole budget reasoning, and answers `finish_reason: "length"` with an *empty* `content`. That
# empty string then fell into the same `bad_model_json` branch a genuinely malformed reply does --
# «модель не удержала формат: » with nothing after the colon, which is not what happened: the
# model never got the chance to hold any format at all.


def test_chat_sends_max_tokens_with_the_measured_default_when_the_provider_has_no_ctx(tmp_path):
    """An external (`type: "openai"`) provider carries no `ctx` in `providers.json` -- there is
    nothing here to cap the default against, so the plain measured default
    (`provider.DEFAULT_MAX_TOKENS`) must reach the wire unchanged."""
    fake = _FakeLlama(chat_payload=_TURN)
    cfg = {"type": "openai", "base_url": f"http://127.0.0.1:{fake.port}", "model": "m"}
    try:
        provider.chat(cfg, {}, [{"role": "user", "content": "x"}])
    finally:
        fake.close()
    (req,) = fake.requests
    assert req["body"]["max_tokens"] == provider.DEFAULT_MAX_TOKENS


def test_chat_caps_the_implicit_max_tokens_default_against_a_small_local_ctx(tmp_path):
    """`llama-local`'s own `ctx` is `n_ctx` -- prompt *and* completion sharing one budget. Blindly
    defaulting to a completion-only budget sized for an external provider (24000) on a `ctx` of
    4096 would leave no room for the prompt at all and trade one broken run for another. Only the
    *implicit* default is capped -- `_llama_cfg`'s `ctx` is 4096, so the sent value must be well
    under `DEFAULT_MAX_TOKENS`, not equal to it.
    """
    fake = _FakeLlama(chat_payload=_TURN)
    try:
        provider.chat(_llama_cfg(fake.port), {}, [{"role": "user", "content": "x"}])
    finally:
        fake.close()
    (req,) = fake.requests
    sent = req["body"]["max_tokens"]
    assert sent == 2048, sent  # max(4096 // 2, 1024)
    assert sent < provider.DEFAULT_MAX_TOKENS


def test_chat_sends_an_explicit_max_tokens_from_providers_json_unchanged(tmp_path):
    """A human wrote `max_tokens` into this provider's own entry -- that is their call, not this
    module's, and it must reach the wire exactly as written even when it is larger than half the
    local `ctx` the capping above would otherwise impose on an *implicit* default."""
    fake = _FakeLlama(chat_payload=_TURN)
    cfg = {**_llama_cfg(fake.port), "max_tokens": 32000}  # well past ctx=4096 // 2 on purpose
    try:
        provider.chat(cfg, {}, [{"role": "user", "content": "x"}])
    finally:
        fake.close()
    (req,) = fake.requests
    assert req["body"]["max_tokens"] == 32000


# -- `max_tokens_param`/`send_temperature`: caila.io's own two divergences ----------------------
#
# caila.io's Anthropic routes (`just-ai/anthropic-claude/...`) silently ignore
# `max_completion_tokens` -- exactly the failure this whole fix round exists to close -- and need
# `max_tokens` instead; an OpenAI reasoning model behind the same host does the opposite and
# rejects `max_tokens` outright, needing `max_completion_tokens`. Never both at once, and the
# choice is a config field, not a guess from the model's name.


def test_max_tokens_param_picks_the_wire_key_the_limit_is_sent_under(tmp_path):
    """The default (`max_tokens`) is what every provider this module already talks to except one
    reasoning model needs -- unchanged by this test. An entry that names a different wire key must
    see the number ride under *that* key, and the default key must then be entirely absent (a
    provider that rejects `max_tokens` outright, as OpenAI's reasoning models do, must not see it
    at all, even alongside the right one)."""
    fake = _FakeLlama(chat_payload=_TURN)
    cfg = {**_llama_cfg(fake.port), "max_tokens_param": "max_completion_tokens", "max_tokens": 999}
    try:
        provider.chat(cfg, {}, [{"role": "user", "content": "x"}])
    finally:
        fake.close()
    (req,) = fake.requests
    assert req["body"]["max_completion_tokens"] == 999
    assert "max_tokens" not in req["body"]


def test_send_temperature_false_omits_temperature_from_the_body(tmp_path):
    """The escape hatch for a provider that rejects `temperature` outright (an OpenAI reasoning
    model, or caila.io on a route from its own documented "инцидент #273") -- `temperature` must
    not appear in the body at all, not even as `null`, when a provider's own entry opts out."""
    fake = _FakeLlama(chat_payload=_TURN)
    cfg = {**_llama_cfg(fake.port), "send_temperature": False, "temperature": 0.9}
    try:
        provider.chat(cfg, {}, [{"role": "user", "content": "x"}])
    finally:
        fake.close()
    (req,) = fake.requests
    assert "temperature" not in req["body"]


def test_send_temperature_defaults_to_true_unchanged_from_before(tmp_path):
    """Regression guard for the flag above: a provider entry that says nothing about
    `send_temperature` at all (every real entry in `providers.json` today) must keep sending
    `temperature` exactly as it did before this field existed."""
    fake = _FakeLlama(chat_payload=_TURN)
    try:
        provider.chat(_llama_cfg(fake.port), {}, [{"role": "user", "content": "x"}])
    finally:
        fake.close()
    (req,) = fake.requests
    assert req["body"]["temperature"] == 0.7


def test_finish_reason_length_is_a_named_truncation_not_bad_model_json(tmp_path):
    """The bug as reproduced: an empty `content` with `finish_reason: "length"` must not read as
    «the model would not hold the schema» -- it never got to try. And retrying is pointless: the
    same `max_tokens` will hit the same wall, so exactly one request goes out, not two."""
    fake = _FakeLlama(chat_payload={
        "choices": [{"message": {"content": ""}, "finish_reason": "length"}]})
    try:
        with pytest.raises(provider.ProviderError) as err:
            provider.chat(_llama_cfg(fake.port), {}, [{"role": "user", "content": "x"}])
        assert err.value.code == "chat_truncated"
        assert err.value.code != "bad_model_json"
        assert len(fake.requests) == 1, "тот же лимит на повторе даст тот же обрыв — не повторяем"
    finally:
        fake.close()


def test_finish_reason_length_wins_even_over_nonempty_but_cut_content(tmp_path):
    """Truncation is not only an empty string -- a partial JSON fragment with
    `finish_reason: "length"` must still be reported as the honest cut, not `bad_model_json`,
    even though `json.loads` on that fragment would also fail and could otherwise fall into the
    same branch a genuinely malformed reply does."""
    fake = _FakeLlama(chat_payload={
        "choices": [{"message": {"content": '{"reply": "почти дописал'},
                     "finish_reason": "length"}]})
    try:
        with pytest.raises(provider.ProviderError) as err:
            provider.chat(_llama_cfg(fake.port), {}, [{"role": "user", "content": "x"}])
        assert err.value.code == "chat_truncated"
    finally:
        fake.close()


def test_empty_content_with_no_truncation_marker_is_still_bad_model_json(tmp_path):
    """The other half of the same fix: an empty (or malformed) reply that carries no
    `finish_reason: "length"` at all -- an ordinary format failure, unrelated to any output-token
    limit -- must keep going through the existing one-retry-then-`bad_model_json` path unchanged.
    """
    fake = _FakeLlama(chat_payload={"choices": [{"message": {"content": ""}}]})
    try:
        with pytest.raises(provider.ProviderError) as err:
            provider.chat(_llama_cfg(fake.port), {}, [{"role": "user", "content": "x"}])
        assert err.value.code == "bad_model_json"
        assert len(fake.requests) == 2, "обычный формат-отказ по-прежнему получает один повтор"
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
                   "slug", "cat-italian-noon",
                   # A8: a keyframe dropped on the chat with no words at all still has to get a
                   # full answer -- a description and a prompt, not a description and a stop.
                   "attaches an image and writes no words"):
        assert anchor in text, anchor


def test_system_prompt_defaults_invented_speech_to_russian_without_touching_preservation():
    """Speech the model makes up itself (the user never supplied a line) must default to
    `[Russian]` -- otherwise it drifts to `[English]`, as it did on the live 2026-08-24 run where
    all six invented lines came back `[English]` for a Russian-speaking user and project. That
    default must not swallow the older preservation rule: a user-supplied line still keeps its own
    language and is never translated, and the user can still ask for a different language and have
    that request win over the Russian default.
    """
    raw = provider.system_prompt().split("## Speech", 1)[1].split("\n## ", 1)[0]
    # collapse markdown line-wrap so a phrase split across two source lines still matches
    section = re.sub(r"\s+", " ", raw)

    # the default sentence must name Russian right where the default itself is stated -- not
    # merely somewhere later in the section, where the language list and the example both mention
    # `[Russian]` too and would keep a wide `invent...default...[Russian]` search green even if
    # the default itself were mutated to name a different language (review found this: mutating
    # `[Russian]` -> `[English]` right here left a `.*`-based search passing on the unrelated
    # `[Russian]` occurrences downstream)
    assert "Default that invented speech to `[Russian]`" in section, \
        "the default sentence no longer names [Russian] as the invented-speech default"

    # the preservation rule for a user-supplied line must still stand, untouched by the new default
    assert "never translate or rewrite it" in section
    assert "applies only to a line the user actually gave you" in section, \
        "preservation rule no longer scoped to user-supplied lines"

    # the default must still explicitly defer to the preservation rule, in that direction -- pin
    # the literal directional sentence itself, not just "preservation" and "default" both being
    # present somewhere nearby, which a flipped ("always overrides") or silently negated sentence
    # would still satisfy (review found this: mutating `never overrides` -> `always overrides` was
    # not caught by any assertion in the previous version of this test)
    assert "it never overrides the preservation rule above" in section, \
        "the default no longer states that it defers to (never overrides) the preservation rule"

    # an explicit request from the user for a language still outranks the Russian default -- pin
    # the literal phrase (no wildcard between subject and verb) so a negation slipped in front of
    # "outranks" -- e.g. "an explicit request never outranks the default", which still contains
    # "outranks the default" as a substring -- cannot pass silently the way the wildcard version did
    assert "an explicit request outranks the default" in section, \
        "no rule letting an explicit user language request override the Russian default"


def test_system_prompt_demands_positive_absence_and_per_scene_accent_color_binding():
    """The 2026-08-24 live run ("Amazon and hoplite") specified full nudity plus one accessory
    each (a crimson cord in her braid, a dented helmet with no crest) and a palette where crimson
    was the only saturated color. By scene 2 the model had drawn loincloths on both fighters that
    the prompt never mentioned, grown a crest on the helmet the prompt explicitly said had none,
    and moved the crimson accent off the cord and onto the invented loincloth. The doc's fix is two
    rules: state an absence positively (not "no clothing" but what specifically is and isn't
    there), and restate which object carries an accent color in every scene, not once -- because
    the visual bible block that carries both is the only thing a downstream scene ever sees of an
    earlier one.
    """
    raw = provider.system_prompt().split("**The visual bible.**", 1)[1].split(
        "## Song mode", 1)[0]
    section = re.sub(r"\s+", " ", raw)

    # rule 1: silence gets read as permission -- absence has to be stated positively
    assert re.search(r"[Ss]tate an absence positively", section), \
        "no rule demanding an absence be stated positively rather than left silent"
    assert re.search(r'no crest and no plume', section), \
        "no concrete positive-absence example (helmet without crest/plume)"

    # rule 2: an accent color has to be re-tied to its object every time, not stated once
    assert re.search(r"[Bb]ind an accent color to the object", section), \
        "no rule binding an accent color to the object carrying it"
    assert re.search(r"[Rr]estate which object the color is on in every scene", section), \
        "no rule requiring the color-object binding to repeat every scene, not just once"


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


# -- Task 5 ("Проекты"): the `project` field ---------------------------------------------------


def test_prompt_schema_carries_an_optional_project_field_outside_required():
    """`project` extends `PROMPT_SCHEMA` the same way `slug` did (A4): outside `required`, so
    every turn this schema ever produced before Task 5 -- none of them carrying the key at all --
    stays a valid answer to this same schema, and an ordinary single-clip editing session never
    has to grow one.
    """
    schema = provider.PROMPT_SCHEMA["schema"]
    assert schema["properties"]["project"]["type"] == ["object", "null"]
    assert "project" not in schema["required"]


def test_project_kind_enum_matches_the_kinds_project_json_itself_uses():
    """One list, not two -- `h3_48gb.project.PROJECT_KINDS` is the source of truth a project.json
    on disk already spells out (Task 1); a schema that independently respelled its own three
    strings could silently drift from it.
    """
    from h3_48gb.project import PROJECT_KINDS
    schema = provider.PROMPT_SCHEMA["schema"]
    assert schema["properties"]["project"]["properties"]["kind"]["enum"] == list(PROJECT_KINDS)


def _project_fields() -> dict:
    return provider.PROMPT_SCHEMA["schema"]["properties"]["project"]["properties"]


def test_project_schema_requires_all_four_keys_present_and_nullable_where_unused():
    """The same shape convention `prompt`'s own fields already use: every key always present,
    `null` standing in for "not this kind's business" -- `scenes` for a `song`, `lyrics`/`caption`
    for a `video`.
    """
    project = provider.PROMPT_SCHEMA["schema"]["properties"]["project"]
    assert sorted(project["required"]) == ["caption", "kind", "lyrics", "scenes"]
    fields = _project_fields()
    assert fields["scenes"]["type"] == ["array", "null"]
    assert fields["lyrics"]["type"] == ["string", "null"]
    assert fields["caption"]["type"] == ["string", "null"]


def test_project_scenes_are_full_prompt_strings_with_a_duration_field():
    scene = _project_fields()["scenes"]["items"]
    assert scene["properties"]["prompt"]["type"] == "string"
    assert scene["properties"]["duration"]["type"] == "number"
    assert scene["required"] == ["prompt", "duration"]


def test_project_scene_duration_is_bounded_to_the_five_to_ten_second_scene_length():
    """Review I1: the brief says scenes are 5-10 s (`docs/h3-prompt-system.md`'s "Scenario mode"),
    but without `minimum`/`maximum` on the schema itself a model that answers `duration: 60` (or
    `2`) passes validation silently and that number rides straight into a GPU `generate` job --
    nothing downstream re-checks it. Validated with `jsonschema` itself, not just dict inspection,
    so this test would actually catch a provider whose completion violates the bound, the same way
    a real `response_format` rejection would.
    """
    import jsonschema

    duration_schema = _project_fields()["scenes"]["items"]["properties"]["duration"]
    assert duration_schema["minimum"] == 5
    assert duration_schema["maximum"] == 10

    def _scene(duration):
        return {"prompt": "x", "duration": duration}

    scene_schema = _project_fields()["scenes"]["items"]
    for bad in (60, 2):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(_scene(bad), scene_schema)
    for good in (5, 7.5, 10):
        jsonschema.validate(_scene(good), scene_schema)


def _video_project_payload() -> dict:
    return {"choices": [{"message": {"content": json.dumps({
        "reply": "вот сценарий из двух сцен",
        "prompt": None,
        "slug": None,
        "project": {
            "kind": "video",
            "scenes": [
                {"prompt": "[Shot 1] Cinematic, a fox crosses a snowy field…", "duration": 8},
                {"prompt": "[Shot 1] Cinematic, the same fox reaches the treeline…",
                 "duration": 6},
            ],
            "lyrics": None,
            "caption": None,
        },
    })}}]}


def test_a_video_scenario_turn_round_trips_through_chat(tmp_path):
    """The video variant: `scenes` carries the sequence, `lyrics`/`caption` stay `null`."""
    fake = _FakeLlama(chat_payload=_video_project_payload())
    try:
        turn = provider.chat(_llama_cfg(fake.port), {}, [{"role": "user", "content": "x"}])
    finally:
        fake.close()
    assert turn["project"]["kind"] == "video"
    assert len(turn["project"]["scenes"]) == 2
    assert turn["project"]["scenes"][0]["duration"] == 8
    assert turn["project"]["lyrics"] is None and turn["project"]["caption"] is None


def _song_project_payload() -> dict:
    return {"choices": [{"message": {"content": json.dumps({
        "reply": "вот лирика и caption",
        "prompt": None,
        "slug": None,
        "project": {
            "kind": "song",
            "scenes": None,
            "lyrics": "[intro]\nТиши, тиши.\n[verse]\nСпи, мой князь.\n[chorus]\nЛуна встаёт.\n"
                      "[outro]\nСпи, мой князь.",
            "caption": "Global Metadata: Wistful lullaby, slow, acoustic.\n\n"
                       "Vocal Details: a mother trying to sound calm while she is not.\n\n"
                       "Arrangement: sparse intro, strings build into the chorus.",
        },
    })}}]}


def test_a_song_scenario_turn_round_trips_through_chat(tmp_path):
    """The song/clip variant: `lyrics`/`caption` carry the song, `scenes` stays `null` -- a
    clip's scenes are only built later, from the finished song's real section timing.
    """
    fake = _FakeLlama(chat_payload=_song_project_payload())
    try:
        turn = provider.chat(_llama_cfg(fake.port), {}, [{"role": "user", "content": "x"}])
    finally:
        fake.close()
    assert turn["project"]["kind"] == "song"
    assert turn["project"]["scenes"] is None
    assert "[intro]" in turn["project"]["lyrics"]
    assert "Arrangement" in turn["project"]["caption"]


def test_a_turn_with_no_project_at_all_is_still_a_valid_parse(tmp_path):
    """The regression guard: `_TURN` is the shape every ordinary video-editing test answers with,
    and it carries no `project` key at all -- `chat` must not choke on its absence, and nothing
    about the single-clip `prompt` path may change.
    """
    fake = _FakeLlama(chat_payload=_TURN)
    try:
        turn = provider.chat(_llama_cfg(fake.port), {}, [{"role": "user", "content": "x"}])
    finally:
        fake.close()
    assert turn.get("project") is None
    assert turn["prompt"]["non_diegetic_music"] == "N/A"


def test_system_prompt_carries_the_scenario_and_song_rules():
    """The rules Task 5's brief pins verbatim: N scenes of 5-10 s connected by a repeated visual
    bible; the "Колыбельная" lessons on `lyrics` (clean structural tags only, no English acting
    directions inside them) and `caption` (three named sections); honest limits on Music3's
    vocal acting; and the suno-import conversion rule.
    """
    text = provider.system_prompt()
    for anchor in (
            # scenario / visual bible
            "visual bible", "5 to 10 seconds", "verbatim",
            # lyrics: clean tags only
            "[intro]", "[pre-chorus]", "[bridge]", "[outro]",
            "Never write an English acting direction inside a tag",
            # caption: the three named sections, in words
            "Global Metadata", "Vocal Details", "Arrangement",
            "emotional frame", "acting task",
            # honest expectations about Music3's vocal acting
            "Music3's vocal", "a real ceiling", "import the finished mp3",
            # suno import conversion
            "Style Prompt", "split at the",
            # review M1: an "Exclude" field converts to an explicit prohibition, not silence
            "Exclude", "explicit prohibition",
            # the schema line itself, extended with `project`
            '"project": object | null'):
        assert anchor in text, anchor


# -- Task 2 ("Сюжет клипа"): SCENARIO_SCHEMA and chat_scenario ----------------------------------
#
# A fourth, separate answer shape -- not `prompt` (one clip), not `project` (a whole video script,
# or lyrics+caption for a song that has not been rendered yet), but `scenario`: the *later* step
# `docs/h3-prompt-system.md`'s own "Song mode" section already promises ("a clip's scenes are only
# built later, from the finished song's actual section timing") -- turning a song that has already
# been rendered, with real section timing (or, for an imported mp3 with no reference lyrics, a raw
# Whisper transcript with per-segment timestamps -- Task 1), into per-section H3 video prompts.
# `SCENARIO_SCHEMA` is deliberately its own top-level schema object, not a fourth key bolted onto
# `PROMPT_SCHEMA` -- the brief is explicit that this must not bloat that schema further.


def _scenario_fields() -> dict:
    return provider.SCENARIO_SCHEMA["schema"]["properties"]["scenario"]["properties"]


def test_scenario_schema_requires_reply_and_a_nullable_scenario_object():
    """The same nullable-but-required shape `PROMPT_SCHEMA`'s own `prompt`/`project` already use:
    `scenario` is `null` while there is nothing to answer with yet (mid-conversation, or a
    clarifying question), but the key itself is always present in a valid answer.
    """
    schema = provider.SCENARIO_SCHEMA["schema"]
    assert schema["required"] == ["reply", "scenario"]
    assert schema["properties"]["reply"]["type"] == "string"
    assert schema["properties"]["scenario"]["type"] == ["object", "null"]
    assert schema["additionalProperties"] is False


def test_scenario_schema_sections_carry_tag_start_end_and_a_scene():
    scenario = provider.SCENARIO_SCHEMA["schema"]["properties"]["scenario"]
    assert sorted(scenario["required"]) == ["sections", "style_block"]
    assert scenario["properties"]["style_block"]["type"] == "string"
    section = _scenario_fields()["sections"]["items"]
    assert sorted(section["required"]) == ["end", "scene", "start", "tag"]
    assert section["properties"]["tag"]["type"] == "string"
    assert section["properties"]["start"]["type"] == "number"
    assert section["properties"]["end"]["type"] == "number"
    scene = section["properties"]["scene"]
    assert scene["required"] == ["prompt", "duration"]
    assert scene["properties"]["prompt"]["type"] == "string"
    assert scene["properties"]["duration"]["type"] == "number"


def test_scenario_schema_scene_carries_an_optional_fresh_start_boolean():
    """P0 fix (keyframe-chain defect, 2026-08-25): `fresh_start` is declared in `scene["properties"]`
    (so `additionalProperties: False` still permits it) but deliberately left OUT of `scene[
    "required"]` -- unlike `PROMPT_SCHEMA`'s own nullable-but-required `prompt`/`project`, there is
    nothing wrong with a model that never writes this key at all (`docs/h3-prompt-system.md`,
    "Breaking the chain on a cast change": most sections continue the same cast/location and never
    need it). `required` staying exactly `["prompt", "duration"]` is the same assertion the
    pre-existing `test_scenario_schema_sections_carry_tag_start_end_and_a_scene` already makes --
    repeated here so a change that quietly adds `fresh_start` to `required` fails this test even if
    that one somehow does not.
    """
    scene = _scenario_fields()["sections"]["items"]["properties"]["scene"]
    assert scene["properties"]["fresh_start"]["type"] == "boolean"
    assert scene["required"] == ["prompt", "duration"]
    assert "fresh_start" not in scene["required"]


def test_scenario_schema_accepts_fresh_start_true_false_or_absent_but_rejects_a_string():
    """jsonschema's own behaviour for a property outside `required`: absent is valid (the model
    just didn't write it), and when present it must actually be a boolean -- a string like `"true"`
    is not silently coerced, it is a schema violation, the same as any other wrong-typed field on
    this entry.
    """
    import jsonschema

    section = {"tag": "verse", "start": 0, "end": 8,
               "scene": {"prompt": "[Shot 1] a lantern-lit nursery", "duration": 8}}
    base = {"reply": "ok", "scenario": {"sections": [section], "style_block": "warm light"}}

    for value in (True, False):
        turn = json.loads(json.dumps(base))
        turn["scenario"]["sections"][0]["scene"]["fresh_start"] = value
        jsonschema.validate(turn, provider.SCENARIO_SCHEMA["schema"])  # must not raise

    jsonschema.validate(base, provider.SCENARIO_SCHEMA["schema"])  # absent key: also valid

    turn = json.loads(json.dumps(base))
    turn["scenario"]["sections"][0]["scene"]["fresh_start"] = "true"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(turn, provider.SCENARIO_SCHEMA["schema"])


def test_scenario_schema_accepts_a_well_formed_turn_and_rejects_bad_ones():
    """The brief's own three checks, all against `jsonschema` itself rather than dict inspection,
    so this would actually catch a completion a real `response_format` rejection would too (review
    I1's own reasoning for `PROMPT_SCHEMA`'s scene `duration`, applied here): a valid scenario
    round-trips; a scene `duration` of 3 or 12 (outside the 5-10 s pipeline ceiling) is rejected;
    a section missing `start` or `end` is rejected.
    """
    import jsonschema

    good = {"reply": "вот сюжет из двух сцен", "scenario": {
        "sections": [
            {"tag": "verse", "start": 0, "end": 8,
             "scene": {"prompt": "[Shot 1] Cinematic, a lantern-lit nursery…", "duration": 8}},
            {"tag": "chorus", "start": 8, "end": 16,
             "scene": {"prompt": "[Shot 1] Cinematic, the same nursery, moonlight…",
                       "duration": 8}},
        ],
        "style_block": "A tired mother in a pale blue nightgown, a wooden crib, warm lamplight.",
    }}
    jsonschema.validate(good, provider.SCENARIO_SCHEMA["schema"])

    for bad_duration in (3, 12):
        turn = json.loads(json.dumps(good))
        turn["scenario"]["sections"][0]["scene"]["duration"] = bad_duration
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(turn, provider.SCENARIO_SCHEMA["schema"])

    for good_duration in (5, 7.5, 10):
        turn = json.loads(json.dumps(good))
        turn["scenario"]["sections"][0]["scene"]["duration"] = good_duration
        jsonschema.validate(turn, provider.SCENARIO_SCHEMA["schema"])

    for missing_key in ("start", "end"):
        turn = json.loads(json.dumps(good))
        del turn["scenario"]["sections"][0][missing_key]
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(turn, provider.SCENARIO_SCHEMA["schema"])


def _scenario_payload() -> dict:
    scenario = {
        "sections": [
            {"tag": "verse", "start": 0, "end": 8,
             "scene": {"prompt": "[Shot 1] Cinematic, a lantern-lit nursery…", "duration": 8}},
            {"tag": "chorus", "start": 8, "end": 16,
             "scene": {"prompt": "[Shot 1] Cinematic, the same nursery, moonlight…",
                       "duration": 8}},
        ],
        "style_block": "A tired mother in a pale blue nightgown, a wooden crib, warm lamplight.",
    }
    return {"choices": [{"message": {"content": json.dumps(
        {"reply": "вот сюжет из двух сцен", "scenario": scenario})}}]}


def test_chat_scenario_sends_its_own_schema_and_returns_a_parsed_scenario(tmp_path):
    fake = _FakeLlama(chat_payload=_scenario_payload())
    try:
        turn = provider.chat_scenario(_llama_cfg(fake.port), {},
                                      [{"role": "user", "content": "x"}])
    finally:
        fake.close()
    assert turn["reply"] == "вот сюжет из двух сцен"
    assert len(turn["scenario"]["sections"]) == 2
    assert turn["scenario"]["sections"][0]["tag"] == "verse"
    assert turn["scenario"]["sections"][0]["scene"]["duration"] == 8
    assert turn["scenario"]["style_block"].startswith("A tired mother")
    (req,) = fake.requests
    assert req["body"]["response_format"]["json_schema"] == provider.SCENARIO_SCHEMA
    assert req["body"]["response_format"]["json_schema"] != provider.PROMPT_SCHEMA


def test_chat_scenario_invalid_model_json_gets_one_retry_then_a_named_error(tmp_path):
    """The same retry-once-then-named-error mechanics `chat` already has (`bad_model_json`, one
    retry, not more) -- shared rather than reimplemented, so this exercises the same code path
    `test_invalid_model_json_gets_one_retry_then_a_named_error` does for `chat`.
    """
    fake = _FakeLlama(chat_payload={"choices": [{"message": {"content": "не json"}}]})
    try:
        with pytest.raises(provider.ProviderError) as err:
            provider.chat_scenario(_llama_cfg(fake.port), {}, [{"role": "user", "content": "x"}])
        assert err.value.code == "bad_model_json"
        assert len(fake.requests) == 2, "должен быть ровно один повтор"
    finally:
        fake.close()


def test_chat_scenario_with_no_provider_listening_raises_the_same_named_error_as_chat(tmp_path):
    fake = _FakeLlama()
    port = fake.port
    fake.close()  # никто больше не слушает этот порт -> connection refused
    with pytest.raises(provider.ProviderError) as err:
        provider.chat_scenario(_llama_cfg(port), {}, [{"role": "user", "content": "x"}])
    assert err.value.code == "chat_unreachable"


def test_chat_scenario_finish_reason_length_is_the_same_named_truncation_as_chat(tmp_path):
    """`_chat_turn` is shared between `chat` and `chat_scenario` -- this is the exact bug report
    the task describes (a full scenario reply is the one shape big enough to actually hit a
    stingy external `max_tokens` default), checked through the scenario entry point rather than
    assumed from `chat`'s own coverage above."""
    fake = _FakeLlama(chat_payload={
        "choices": [{"message": {"content": ""}, "finish_reason": "length"}]})
    try:
        with pytest.raises(provider.ProviderError) as err:
            provider.chat_scenario(_llama_cfg(fake.port), {}, [{"role": "user", "content": "x"}])
        assert err.value.code == "chat_truncated"
        assert len(fake.requests) == 1
    finally:
        fake.close()


def test_system_prompt_carries_the_clip_scenario_section():
    """Task 2's own anchors: the new "Clip scenario mode" section teaches the model the fourth
    answer shape (`scenario`, validated by `SCENARIO_SCHEMA`) -- its input (lyrics or a raw
    timestamped transcript, the song's `caption`, the track's `duration`), its output (sections
    covering the whole track, one scene per section), and the rules a real generation needs: no
    sung close-ups (with the actual reason -- H3 never hears the song and the real mix replaces
    its audio in post, so lip-sync cannot exist), the visual bible copied verbatim into every
    scene, audio negatives staying out of the video prompt, and imagery drawn from a section's
    meaning rather than its exact, possibly misheard, words.
    """
    text = provider.system_prompt()
    for anchor in (
            "Clip scenario mode",
            "SCENARIO_SCHEMA",
            "a raw transcript with no such tags",
            "Whisper",
            "not for karaoke",
            "the first section's `start` is `0`",
            "close-up on a face that is singing",
            "H3 never hears this",
            "replaced by the track's actual mastered mix",
            "verbatim, word for word",
            "Audio negatives",
            "meaning, not from restaging",
    ):
        assert anchor in text, anchor


# -- stream: caila.io idle-drop workaround (Task 4) --------------------------------------------
#
# Measured, not assumed: a full-song `SCENARIO_SCHEMA` request to `claude-opus-5` behind caila.io
# was cut with `RemoteDisconnected: Remote end closed connection without response` at 7m16s; the
# same call with a shorter brief passed uncut at 226s. `cfg["stream"]` (default `False`, an
# escape hatch a provider opts into -- unchanged for every provider already in `providers.json`)
# switches `_chat_turn` to Server-Sent Events so bytes keep moving on the wire for the whole
# ~20k-token reply. `_read_sse` is the parser; these tests exercise it only through `chat`/
# `chat_scenario`, the same way every other `_chat_turn` mechanic in this file already is.


def _sse(*chunks) -> list:
    """`stream_chunks` for `_FakeLlama`: one `data:` frame per positional `dict`, terminated by
    an explicit `[DONE]` frame -- the common case every test below wants unless it is deliberately
    testing what happens *without* one.
    """
    return [*chunks, "data: [DONE]\n\n"]


def _delta_chunk(content: str | None = None, finish_reason: str | None = None,
                 reasoning: str | None = None) -> dict:
    delta = {}
    if content is not None:
        delta["content"] = content
    if reasoning is not None:
        delta["reasoning_content"] = reasoning
    choice = {"delta": delta}
    if finish_reason is not None:
        choice["finish_reason"] = finish_reason
    return {"choices": [choice]}


def test_stream_true_in_providers_json_puts_stream_true_on_the_wire(tmp_path):
    """The config flag has to actually reach the request body -- checked by reading what the mock
    server recorded, not by trusting the code path that claims to set it."""
    content = _TURN["choices"][0]["message"]["content"]
    fake = _FakeLlama(stream_chunks=_sse(_delta_chunk(content, finish_reason="stop")))
    try:
        provider.chat({**_llama_cfg(fake.port), "stream": True}, {},
                     [{"role": "user", "content": "x"}])
    finally:
        fake.close()
    (req,) = fake.requests
    assert req["body"]["stream"] is True


def test_stream_omitted_by_default_leaves_the_body_unchanged(tmp_path):
    """The other half of the same flag: a provider entry that says nothing about `stream` (every
    real entry in `providers.json` today) must not carry the key at all -- not even as `false` --
    so the non-streaming path this fix must not break stays byte-for-byte what it was before."""
    fake = _FakeLlama(chat_payload=_TURN)
    try:
        provider.chat(_llama_cfg(fake.port), {}, [{"role": "user", "content": "x"}])
    finally:
        fake.close()
    (req,) = fake.requests
    assert "stream" not in req["body"]


def test_stream_chunks_concatenate_into_the_whole_reply(tmp_path):
    """The point of SSE here is that no single chunk carries the whole answer -- a real 20k-token
    reply arrives over hundreds of them. Split one valid turn's JSON across three separate `data:`
    frames and check `chat` hands back the exact same parsed turn `_TURN` itself would, not a
    fragment of it."""
    content = _TURN["choices"][0]["message"]["content"]
    third = len(content) // 3
    parts = [content[:third], content[third:2 * third], content[2 * third:]]
    assert "".join(parts) == content, "sanity: the split must not lose a byte"
    fake = _FakeLlama(stream_chunks=_sse(
        _delta_chunk(parts[0]),
        _delta_chunk(parts[1]),
        _delta_chunk(parts[2], finish_reason="stop"),
    ))
    try:
        turn = provider.chat({**_llama_cfg(fake.port), "stream": True}, {},
                             [{"role": "user", "content": "x"}])
    finally:
        fake.close()
    assert turn["reply"] == "Сделал мрачнее."
    assert turn["prompt"]["integrated_multimodal_description"] == "[Shot 1] Live-action…"


def test_stream_done_ends_the_stream_and_later_frames_are_never_read(tmp_path):
    """`[DONE]` is the honest end of the answer -- content in a `data:` frame written after it must
    never reach the accumulated reply. Proven by putting a frame that, if read, would corrupt the
    otherwise-valid JSON after `[DONE]`."""
    content = _TURN["choices"][0]["message"]["content"]
    fake = _FakeLlama(stream_chunks=[
        f"data: {json.dumps(_delta_chunk(content, finish_reason='stop'))}\n\n",
        "data: [DONE]\n\n",
        # If this were read, it would append garbage after the closing brace and break json.loads.
        f"data: {json.dumps(_delta_chunk('GARBAGE-AFTER-DONE'))}\n\n",
    ])
    try:
        turn = provider.chat({**_llama_cfg(fake.port), "stream": True}, {},
                             [{"role": "user", "content": "x"}])
    finally:
        fake.close()
    assert turn["reply"] == "Сделал мрачнее."


def test_stream_reasoning_deltas_never_reach_the_accumulated_content(tmp_path):
    """A reasoning model may stream `reasoning`/`reasoning_content`/`thinking` deltas before its
    real answer (ai-writer 2.0's own ADR-065 warning, applied here) -- none of the three may end
    up concatenated into `content`, which must contain only the actual JSON reply.

    All three key spellings are exercised, not just two: review round 2's cheap findings flagged
    that the original version of this test covered `reasoning_content` (via `_delta_chunk`'s own
    `reasoning=` kwarg) and `thinking` (the hand-built dict below) but never literal `reasoning`,
    even though `_read_sse`'s own docstring names it as a real spelling some providers use --
    clean coverage, since it cannot actually leak given the implementation only ever reads
    `delta["content"]`, but a docstring naming three things and a test exercising two is exactly
    the kind of gap this file's own mutation-testing rule exists to close.
    """
    content = _TURN["choices"][0]["message"]["content"]
    fake = _FakeLlama(stream_chunks=_sse(
        _delta_chunk(reasoning="Дай подумаю, как лучше ответить..."),
        {"choices": [{"delta": {"thinking": "ещё немного думаю"}}]},
        {"choices": [{"delta": {"reasoning": "и вот что я решила"}}]},
        _delta_chunk(content, finish_reason="stop"),
    ))
    try:
        turn = provider.chat({**_llama_cfg(fake.port), "stream": True}, {},
                             [{"role": "user", "content": "x"}])
    finally:
        fake.close()
    # The reasoning text must not have leaked into the reply -- not as a prefix, not anywhere.
    assert "думаю" not in turn["reply"]
    assert "решила" not in turn["reply"]
    assert "думаю" not in json.dumps(turn, ensure_ascii=False)
    assert "решила" not in json.dumps(turn, ensure_ascii=False)
    assert turn["reply"] == "Сделал мрачнее."


def test_stream_finish_reason_length_in_the_last_chunk_gives_chat_truncated(tmp_path):
    """The same honest distinction the non-streaming path already makes (see the `chat_truncated`
    fix-round tests above) must survive the wire-shape change: a stream whose last chunk carries
    `finish_reason: "length"` is a cut reply, not a malformed one -- `chat_truncated`, not
    `bad_model_json`, and not retried (the same `max_tokens` would hit the same wall)."""
    fake = _FakeLlama(stream_chunks=_sse(
        _delta_chunk('{"reply": "почти дописал'),
        _delta_chunk(finish_reason="length"),
    ))
    try:
        with pytest.raises(provider.ProviderError) as err:
            provider.chat({**_llama_cfg(fake.port), "stream": True}, {},
                         [{"role": "user", "content": "x"}])
        assert err.value.code == "chat_truncated"
        assert err.value.code != "bad_model_json"
        assert len(fake.requests) == 1, "тот же лимит на повторе даст тот же обрыв — не повторяем"
    finally:
        fake.close()


class _CutIter:
    """A fake `r` for `_read_sse` that yields a few lines and then simply stops -- exactly what
    `_read_sse`'s `for raw_line in r:` sees once a real socket's connection is cut mid-response,
    without going through a real loopback TCP connection to get there.

    Tried the real-socket route first: `_FakeLlama` answers a short `stream_chunks` list with no
    `[DONE]`, over `ThreadingHTTPServer`'s ordinary "handler returns -> framework closes the
    connection" teardown. It is genuinely racy -- five back-to-back runs of that exact scenario
    against a real loopback socket came back as one clean success, two `ConnectionResetError`s and
    two multi-second hangs before the read finally timed out, all from the *same* code and the
    *same* machine, only the OS's own scheduling of the accept thread's teardown differed run to
    run. That race is a property of driving a real socket race from a lightweight test server, not
    of the code under test -- `_read_sse`'s own EOF branch runs identically no matter what kind of
    object it is iterating, so this is what actually gets exercised, deterministically and in
    microseconds rather than seconds.
    """

    def __init__(self, lines: list[bytes]):
        self._lines = lines

    def __iter__(self):
        return iter(self._lines)


def test_stream_cut_before_any_finish_reason_is_chat_unreachable_not_a_partial_parse():
    """The connection ending mid-answer -- no `[DONE]`, no `finish_reason` at all -- must be a
    named, honest refusal and must not hand back the JSON fragment collected so far as if it were
    a real (if malformed) reply: `chat_unreachable`, never `bad_model_json`, and the fragment
    itself must not appear in the error message as if it had been parsed."""
    lines = [
        (f"data: {json.dumps(_delta_chunk('{\"reply\": \"почти'))}\n").encode(),
        b"\n",
        # No finish_reason chunk, no [DONE] -- the iterable just ends here, as a cut socket would.
    ]
    with pytest.raises(provider.ProviderError) as err:
        provider._read_sse(_CutIter(lines))
    assert err.value.code == "chat_unreachable"
    assert err.value.code != "bad_model_json"
    assert "почти" not in str(err.value), "half-collected content must not read as if parsed"


def test_stream_non_json_data_frame_is_a_named_bad_provider_reply_not_a_silent_crash(tmp_path):
    """A `data:` line whose payload is not JSON breaks the wire protocol mid-stream -- the same
    class of failure the non-streaming path's `bad_provider_reply` already names for "a 200 that
    is not a completion". Must not raise an unnamed `json.JSONDecodeError` straight out of this
    module, and must not be retried (the malformed frame is not the model failing to hold a
    schema)."""
    fake = _FakeLlama(stream_chunks=[
        "data: это не json совсем\n\n",
        "data: [DONE]\n\n",
    ])
    try:
        with pytest.raises(provider.ProviderError) as err:
            provider.chat({**_llama_cfg(fake.port), "stream": True}, {},
                         [{"role": "user", "content": "x"}])
        assert err.value.code == "bad_provider_reply"
        assert len(fake.requests) == 1, "сломанный кадр — не повод переспрашивать"
    finally:
        fake.close()


def test_stream_invalid_model_json_gets_one_retry_then_a_named_error(tmp_path):
    """The one-retry-then-named-error mechanic (`test_invalid_model_json_gets_one_retry_then_a_
    named_error` for the non-streaming path) must survive the wire-shape change: a stream whose
    accumulated `content` is not valid JSON, with no truncation marker, gets exactly one retry
    before `bad_model_json` -- checked by request count, so a retry that silently stopped
    happening would be caught."""
    fake = _FakeLlama(stream_chunks=_sse(_delta_chunk("не json", finish_reason="stop")))
    try:
        with pytest.raises(provider.ProviderError) as err:
            provider.chat({**_llama_cfg(fake.port), "stream": True}, {},
                         [{"role": "user", "content": "x"}])
        assert err.value.code == "bad_model_json"
        assert len(fake.requests) == 2, "должен быть ровно один повтор"
    finally:
        fake.close()


def test_stream_also_works_through_chat_scenario_the_shared_code_path(tmp_path):
    """`_chat_turn` is shared between `chat` and `chat_scenario` -- checked through the scenario
    entry point rather than assumed from `chat`'s own coverage above, the same way the
    `finish_reason: "length"` fix round checks both entry points separately."""
    payload = _scenario_payload()
    content = payload["choices"][0]["message"]["content"]
    fake = _FakeLlama(stream_chunks=_sse(_delta_chunk(content, finish_reason="stop")))
    try:
        turn = provider.chat_scenario({**_llama_cfg(fake.port), "stream": True}, {},
                                      [{"role": "user", "content": "x"}])
    finally:
        fake.close()
    assert turn["scenario"]["sections"][0]["tag"] == "verse"
    (req,) = fake.requests
    assert req["body"]["stream"] is True


# -- review round 2: I1 -- a provider's own mid-stream refusal, or a stream request answered with
# a plain body, must not read as "nobody answered" -----------------------------------------------


def test_stream_error_chunk_mid_stream_is_a_named_bad_provider_reply_not_chat_unreachable(
        tmp_path):
    """OpenRouter's own documented shape for an upstream failure *mid-stream* -- a `data:` event
    carrying `error` instead of `choices` -- must be read as the provider's own explanation
    (`bad_provider_reply`, the same code the non-streaming 200-with-`{"error": ...}` envelope
    already gets), not silently skipped until the stream runs dry and reports `chat_unreachable`
    ("check the address and that it's running") for a provider that was up and said exactly what
    was wrong (review round 2, I1)."""
    fake = _FakeLlama(stream_chunks=[
        f"data: {json.dumps({'error': {'message': 'upstream provider is overloaded'}})}\n\n",
        # A [DONE] follows even though the correct implementation never reaches it (the raise
        # happens on the error chunk itself) -- without it, a *mutated* implementation that
        # dropped the error-chunk check would fall through to reading the real socket to EOF with
        # no [DONE] and no finish_reason, which turned out to be exactly as racy against this
        # HTTP/1.0 mock server as the earlier SSE cut-stream case (`_CutIter`'s own docstring,
        # above) -- sometimes fast, sometimes a multi-second hang. Terminating cleanly here keeps
        # the mutation check (and this test, on a slow CI box) fast and deterministic either way.
        "data: [DONE]\n\n",
    ])
    try:
        with pytest.raises(provider.ProviderError) as err:
            provider.chat({**_llama_cfg(fake.port), "stream": True}, {},
                         [{"role": "user", "content": "x"}])
        assert err.value.code == "bad_provider_reply"
        assert err.value.code != "chat_unreachable"
        assert "overloaded" in str(err.value)
        assert len(fake.requests) == 1, "провайдер объяснился -- не повод переспрашивать"
    finally:
        fake.close()


def test_stream_true_but_provider_ignores_it_and_answers_plain_json_still_returns_the_turn(
        tmp_path):
    """A provider that does not actually support streaming and answers one ordinary JSON body
    anyway (`Content-Type: application/json`, no `stream_chunks` configured on the mock at all)
    must still be read as a normal completion -- not run through `_read_sse`, which would find no
    line starting with `data:`, reach EOF with no `finish_reason`, and report `chat_unreachable`
    for a provider that was up and had, in fact, answered (review round 2, I1)."""
    fake = _FakeLlama(chat_payload=_TURN)  # no stream_chunks: falls through to the ordinary body
    try:
        turn = provider.chat({**_llama_cfg(fake.port), "stream": True}, {},
                             [{"role": "user", "content": "x"}])
    finally:
        fake.close()
    assert turn["reply"] == "Сделал мрачнее."
    (req,) = fake.requests
    assert req["body"]["stream"] is True, "запрос всё равно попросил поток -- ответил провайдер"


# -- review round 2: I2 -- `content: null` must not crash the `bad_model_json` message itself ----


def test_bad_model_json_message_does_not_crash_when_content_is_json_null(tmp_path):
    """A reasoning model can answer `content: null` (valid JSON; `json.loads(None)` is a
    `TypeError`, exactly what the existing `except (json.JSONDecodeError, TypeError)` around
    `raw2` is there for) with a `finish_reason` that is not `"length"`. The message-building line
    itself used to slice `raw2` unconditionally (`raw2[:400]`), which crashes with an unnamed
    `TypeError: 'NoneType' object is not subscriptable` on exactly this input -- reaching the page
    as a bare 500 «сервер споткнулся» instead of the named `bad_model_json` this whole branch
    exists to produce (review round 2, I2)."""
    fake = _FakeLlama(chat_payload={"choices": [{"message": {"content": None}}]})
    try:
        with pytest.raises(provider.ProviderError) as err:
            provider.chat(_llama_cfg(fake.port), {}, [{"role": "user", "content": "x"}])
        assert err.value.code == "bad_model_json"
        assert len(fake.requests) == 2, "обычный формат-отказ по-прежнему получает один повтор"
    finally:
        fake.close()


# -- review round 2: I3 -- an HTTP error status is the provider answering, not nobody answering --


def test_http_error_status_is_a_named_bad_provider_reply_not_chat_unreachable(tmp_path):
    """A 4xx/5xx with the provider's own explanation ("max_tokens is not supported for this
    model" -- a live shape this exact review round's own new fields, `max_tokens_param` and
    `send_temperature`, are the most likely thing to produce if misconfigured) must read as the
    provider answering, not as nobody answering at all -- `bad_provider_reply`, never
    `chat_unreachable` (review round 2, I3). The body is deliberately not echoed (I4's own
    reasoning: an HTTP error body from an arbitrary host is exactly the shape a misconfigured
    proxy could use to echo request headers back) -- only the status and reason phrase ride in
    the message."""
    fake = _FakeLlama(chat_payload={"error": "max_tokens is not supported for this model"},
                      chat_status=400)
    try:
        with pytest.raises(provider.ProviderError) as err:
            provider.chat(_llama_cfg(fake.port), {}, [{"role": "user", "content": "x"}])
        assert err.value.code == "bad_provider_reply"
        assert err.value.code != "chat_unreachable"
        assert "400" in str(err.value)
        assert "max_tokens is not supported" not in str(err.value), "тело не эхо -- см. I4"
        assert len(fake.requests) == 1
    finally:
        fake.close()


def test_gateway_status_from_an_http_error_is_still_chat_unreachable(tmp_path):
    """502/503/504 are gateway/proxy-level codes, not an answer from the origin provider itself --
    discovered directly on this development machine, whose own system HTTP proxy answers a bare
    502 for a connection nobody is listening on (`urllib.request.getproxies()` picks it up
    automatically, outside any test's control). Without this carve-out in `ask()`'s `HTTPError`
    branch, that 502 read as `bad_provider_reply` -- "the provider answered" -- when in fact
    nothing at `_base_url` ever did, and the two pre-existing "nobody is listening" tests
    (`test_chat_with_no_provider_listening_raises_a_named_error` and its `chat_scenario` twin)
    would regress on exactly this machine if this carve-out were ever removed."""
    fake = _FakeLlama(chat_payload={"error": "different host, different failure"}, chat_status=502)
    try:
        with pytest.raises(provider.ProviderError) as err:
            provider.chat(_llama_cfg(fake.port), {}, [{"role": "user", "content": "x"}])
        assert err.value.code == "chat_unreachable"
        assert err.value.code != "bad_provider_reply"
    finally:
        fake.close()


# -- review round 2: I4 -- an echoed bearer token must not ride in a `bad_provider_reply` ---------


def test_bad_provider_reply_redacts_an_echoed_bearer_token_non_stream(tmp_path):
    """M3 (see `test_provider`'s own docstring, and `test_a_two_hundred_carrying_a_providers_own_
    error_is_a_named_refusal`'s neighbourhood) closed this exact vector for `test_provider`'s own
    `detail`: a misconfigured proxy in front of a provider can echo *request* headers, the bearer
    token among them, back in a 200 body. `_chat_turn`'s own `bad_provider_reply` deliberately
    keeps echoing the provider's body (unlike the probe -- diagnostic text a person debugging an
    actual broken chat turn needs) -- but must not keep echoing a bearer token specifically if the
    echoed body happens to carry one (review round 2, I4)."""
    fake = _FakeLlama(chat_payload={
        "echo": {"headers": {"Authorization": "Bearer sk-real-secret-abc123"}}})
    try:
        with pytest.raises(provider.ProviderError) as err:
            provider.chat(_llama_cfg(fake.port), {}, [{"role": "user", "content": "x"}])
        assert err.value.code == "bad_provider_reply"
        assert "sk-real-secret-abc123" not in str(err.value)
        assert "Bearer" in str(err.value), "текст остаётся диагностическим -- не пустой вырез"
    finally:
        fake.close()


def test_stream_bad_provider_reply_redacts_an_echoed_bearer_token(tmp_path):
    """Same vector, over the streaming error-chunk path added for I1 above -- a proxy that echoes
    request headers back inside `data: {"error": ...}` must not leak the token either."""
    fake = _FakeLlama(stream_chunks=[
        f"data: {json.dumps({'error': {'echo': 'Authorization: Bearer sk-stream-secret-xyz'}})}"
        f"\n\n",
        # Same reason as the sibling test above: keeps a mutated implementation's fallback path
        # off the racy real-socket-EOF timing instead of leaving it to chance.
        "data: [DONE]\n\n",
    ])
    try:
        with pytest.raises(provider.ProviderError) as err:
            provider.chat({**_llama_cfg(fake.port), "stream": True}, {},
                         [{"role": "user", "content": "x"}])
        assert err.value.code == "bad_provider_reply"
        assert "sk-stream-secret-xyz" not in str(err.value)
        assert "Bearer" in str(err.value)
    finally:
        fake.close()


def test_bad_provider_reply_redaction_does_not_swallow_the_json_tail_after_the_token(tmp_path):
    r"""Review round 3 (Minor, reproduced on real code, not a mock): the original
    `_BEARER_TOKEN_RE` used `\S+` for the token itself -- "everything up to the next whitespace".
    `Authorization` is often the *last* key in an echoed `headers` object (exactly the shape used
    above, and the realistic one: JSON key order commonly puts it last), so the closing quote and
    every closing brace after the token sit right next to it with no whitespace in between --
    `\S+` swallowed all of that along with the token. The secret never leaked (a property the two
    tests above already pin) -- but the message this left behind was not the diagnostic, still-
    readable text `bad_provider_reply`'s own contract promises: it stopped mid-structure, with no
    closing quote or brace at all, which is not what the docstring of `_redact_secrets` (and its
    neighbour comment on `_BEARER_TOKEN_RE`) claims -- "the diagnostic value of the body remains."
    This pins the actual tail, not just the absence of the secret: the redacted body must still be
    valid, parseable JSON, closing exactly where the original did.
    """
    fake = _FakeLlama(chat_payload={
        "echo": {"headers": {"Authorization": "Bearer sk-real-secret-abc123"}}})
    try:
        with pytest.raises(provider.ProviderError) as err:
            provider.chat(_llama_cfg(fake.port), {}, [{"role": "user", "content": "x"}])
    finally:
        fake.close()
    message = str(err.value)
    assert "sk-real-secret-abc123" not in message
    body = message[message.index("{"):]
    assert body.endswith('"}}}'), body
    assert json.loads(body) == {"echo": {"headers": {"Authorization": "Bearer [скрыто]"}}}


# -- review round 2: cheap -- `http.client.HTTPException` was an untested line -------------------


def test_incomplete_read_mid_response_is_the_same_named_chat_unreachable():
    """`http.client.HTTPException` (`IncompleteRead` among others) is not an `OSError` -- without
    it in `ask()`'s transport `except` clause, a connection cut mid-response (fewer bytes than the
    provider's own `Content-Length` promised) would escape as a raw, unnamed exception instead of
    the same honest `chat_unreachable` an outright-refused connection already gets. Flagged in
    review round 2 as an untested line: removing it from the `except` tuple broke no test.

    Proven directly against `urlopen` rather than a real socket: forcing a real connection to
    supply fewer bytes than its own `Content-Length` promised turned out to be exactly as racy as
    the earlier SSE cut-stream case (`_CutIter`'s own docstring, above) -- sometimes a clean
    `IncompleteRead`, sometimes a `ConnectionResetError` that the pre-existing `OSError` branch
    would have caught regardless, leaving it genuinely unclear whether this branch was ever
    exercised. Patching `urlopen` to raise the exact exception removes the race and the ambiguity.
    """
    with patch("h3_48gb.provider.urllib.request.urlopen",
              side_effect=http.client.IncompleteRead(b"partial")):
        with pytest.raises(provider.ProviderError) as err:
            provider.chat(_llama_cfg(1), {}, [{"role": "user", "content": "x"}])
    assert err.value.code == "chat_unreachable"


# -- review round 2: cheap -- `max_tokens_param` typos went out on the wire silently --------------


def test_max_tokens_param_with_an_unknown_wire_key_is_a_named_config_refusal(tmp_path):
    """A typo in `max_tokens_param` (`"maxTokens"` instead of `max_tokens`) does not fail on the
    wire -- an OpenAI-shaped API just ignores an unrecognised JSON key -- so without this check
    the request would go out with no real limit set at all, and the resulting truncation would
    blame a parameter that was already correct (review round 2, I-cheap). Checked, and refused by
    name, before a single request goes out."""
    fake = _FakeLlama(chat_payload=_TURN)
    cfg = {**_llama_cfg(fake.port), "max_tokens_param": "maxTokens"}
    try:
        with pytest.raises(provider.ProviderError) as err:
            provider.chat(cfg, {}, [{"role": "user", "content": "x"}])
        assert err.value.code == "bad_provider_config"
        assert "maxTokens" in str(err.value)
        assert len(fake.requests) == 0, "конфиг сломан -- переспрашивать нечего"
    finally:
        fake.close()


def test_max_tokens_param_known_values_are_unaffected_by_the_new_check(tmp_path):
    """Regression guard for the check above: both values this file actually knows how to send
    must keep working exactly as the earlier `max_tokens_param`/`send_temperature` fix round left
    them."""
    fake = _FakeLlama(chat_payload=_TURN)
    try:
        provider.chat({**_llama_cfg(fake.port), "max_tokens_param": "max_completion_tokens"}, {},
                     [{"role": "user", "content": "x"}])
    finally:
        fake.close()
    (req,) = fake.requests
    assert "max_completion_tokens" in req["body"]


# -- review round 2: accepted-but-cheap -- honest wording when `ctx`, not `max_tokens`, capped it -


def test_chat_truncated_message_blames_ctx_when_the_cap_came_from_it_not_max_tokens(tmp_path):
    """When the effective `max_tokens` came from the `ctx`-derived cap (no explicit `max_tokens`
    written for this provider), "raise `max_tokens`" sent a person to add a field that was not
    the cause and, on its own, would not have changed anything -- the field actually governing
    the limit here is `ctx`. The message must name it."""
    fake = _FakeLlama(chat_payload={
        "choices": [{"message": {"content": ""}, "finish_reason": "length"}]})
    try:
        with pytest.raises(provider.ProviderError) as err:
            provider.chat(_llama_cfg(fake.port), {}, [{"role": "user", "content": "x"}])
        assert err.value.code == "chat_truncated"
        assert "ctx" in str(err.value)
    finally:
        fake.close()


def test_chat_truncated_message_still_blames_max_tokens_when_it_was_set_explicitly(tmp_path):
    """Regression guard: a provider with an explicit `max_tokens` in `providers.json` (not
    `ctx`-capped, even though `ctx` is also present in the same entry) must keep the original,
    unmodified advice."""
    fake = _FakeLlama(chat_payload={
        "choices": [{"message": {"content": ""}, "finish_reason": "length"}]})
    cfg = {**_llama_cfg(fake.port), "max_tokens": 500}
    try:
        with pytest.raises(provider.ProviderError) as err:
            provider.chat(cfg, {}, [{"role": "user", "content": "x"}])
        assert err.value.code == "chat_truncated"
        assert "поднимите `max_tokens`" in str(err.value)
    finally:
        fake.close()
