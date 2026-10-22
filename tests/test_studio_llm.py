"""The studio's LLM door: the wire carries the caller's schema; config and GPU-lock refusals."""
from __future__ import annotations

import fcntl
import json
import os

import pytest
from _fake_llama import _FakeLlama

from h3_48gb import provider
from h3_48gb.studio.errors import StudioError
from h3_48gb.studio.llm import ProviderLlm, gpu_lock

SCHEMA = {"name": "studio_probe", "schema": {
    "type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"],
    "additionalProperties": False}}


def _reply(obj):
    return {"choices": [{"message": {"content": json.dumps(obj)}, "finish_reason": "stop"}]}


def test_chat_with_schema_sends_the_callers_schema(tmp_path):
    fake = _FakeLlama(chat_payload=_reply({"x": "ok"}))
    try:
        cfg = {"type": "openai", "base_url": f"http://127.0.0.1:{fake.port}", "model": "m"}
        got = provider.chat_with_schema(cfg, {}, [{"role": "user", "content": "hi"}], SCHEMA,
                                        "only JSON")
    finally:
        fake.close()
    assert got == {"x": "ok"}
    (req,) = fake.requests
    assert req["body"]["response_format"] == {"type": "json_schema", "json_schema": SCHEMA}


def _home_with(tmp_path, providers: dict):
    home = tmp_path / "studio"
    home.mkdir()
    (home / "providers.json").write_text(json.dumps(providers))
    return home


def test_provider_llm_talks_to_the_active_provider(tmp_path):
    fake = _FakeLlama(chat_payload=_reply({"x": "да"}))
    try:
        home = _home_with(tmp_path, {"active": "f", "providers": {"f": {
            "type": "openai", "base_url": f"http://127.0.0.1:{fake.port}", "model": "m",
            "shares_gpu": False}}})
        llm = ProviderLlm(home, tmp_path / "gpu-mac.lock")
        assert llm.json_turn([{"role": "user", "content": "q"}], SCHEMA, "r") == {"x": "да"}
        assert llm.label == "f:m"
    finally:
        fake.close()


def test_no_provider_is_a_named_503(tmp_path):
    llm = ProviderLlm(tmp_path / "empty", tmp_path / "gpu-mac.lock")
    with pytest.raises(StudioError) as err:
        llm.json_turn([], SCHEMA, "r")
    assert (err.value.status, err.value.code) == (503, "llm_not_configured")


def test_provider_failure_is_a_named_502(tmp_path):
    home = _home_with(tmp_path, {"active": "f", "providers": {"f": {
        "type": "openai", "base_url": "http://127.0.0.1:9", "model": "m", "shares_gpu": False}}})
    with pytest.raises(StudioError) as err:
        ProviderLlm(home, tmp_path / "gpu-mac.lock").json_turn([], SCHEMA, "r")
    assert (err.value.status, err.value.code, err.value.message, err.value.detail) == \
        (502, "llm_failed", "Сценарист недоступен: провайдер не ответил",
         {"provider": "f", "provider_code": "chat_unreachable"})


def test_gpu_lock_held_elsewhere_is_409_gpu_busy(tmp_path):
    lock = tmp_path / "gpu-mac.lock"
    fd = os.open(lock, os.O_RDWR | os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        with pytest.raises(StudioError) as err:
            with gpu_lock(lock, enabled=True, timeout_s=0.3):
                pass
        assert (err.value.status, err.value.code) == (409, "gpu_busy")
        with gpu_lock(lock, enabled=False, timeout_s=0.3):
            pass                        # a provider off the GPU never waits for the render
    finally:
        os.close(fd)


def test_a_gpu_sharing_provider_waits_for_the_render_lock(tmp_path):
    """§8.4 local-mac: the request never reaches a provider that shares the GPU with the render."""
    fake = _FakeLlama(chat_payload=_reply({"x": "ok"}))
    lock = tmp_path / "gpu-mac.lock"
    fd = os.open(lock, os.O_RDWR | os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        home = _home_with(tmp_path, {"active": "f", "providers": {"f": {
            "type": "openai", "base_url": f"http://127.0.0.1:{fake.port}", "model": "m",
            "shares_gpu": True}}})
        with pytest.raises(StudioError) as err:
            ProviderLlm(home, lock, lock_timeout_s=0.3).json_turn([], SCHEMA, "r")
        assert (err.value.status, err.value.code, fake.requests) == (409, "gpu_busy", [])
    finally:
        os.close(fd)
        fake.close()


def test_a_provider_without_its_token_is_a_named_503(tmp_path):
    home = _home_with(tmp_path, {"active": "caila", "providers": {"caila": {
        "type": "openai", "base_url": "http://127.0.0.1:9", "model": "m",
        "api_key_env": "CAILA_API_KEY"}}})
    with pytest.raises(StudioError) as err:
        ProviderLlm(home, tmp_path / "gpu-mac.lock").json_turn([], SCHEMA, "r")
    assert (err.value.status, err.value.code, err.value.message) == (
        503, "llm_not_configured", "Сценарист не настроен: нет токена CAILA_API_KEY")


SECRET = "sk-SYNTHETIC-SECRET-0042"


@pytest.mark.parametrize("payload, code", [
    # the model's own text is not JSON and quotes the key: provider's bad_model_json echoes it
    ({"choices": [{"message": {"content": f"not json, key {SECRET}"}}]}, "bad_model_json"),
    # a 200 envelope that is not a completion, echoing the key with and without "Bearer"
    ({"error": {"message": f"Authorization: Bearer {SECRET}; key={SECRET}"}}, "bad_provider_reply"),
])
def test_provider_text_never_leaves_the_studio(tmp_path, caplog, payload, code):
    """No provider message, raw model reply or token reaches the error body or the log."""
    fake = _FakeLlama(chat_payload=payload)
    try:
        home = _home_with(tmp_path, {"active": "f", "providers": {"f": {
            "type": "openai", "base_url": f"http://127.0.0.1:{fake.port}", "model": "m",
            "api_key_env": "KEY", "shares_gpu": False}}})
        (home / ".env").write_text(f"KEY={SECRET}\n")
        with pytest.raises(StudioError) as err:
            ProviderLlm(home, tmp_path / "gpu-mac.lock").json_turn([], SCHEMA, "r")
    finally:
        fake.close()
    assert err.value.body() == {"error": {
        "code": "llm_failed", "message": SAFE_MESSAGES[code],
        "detail": {"provider": "f", "provider_code": code}}}
    assert err.value.__cause__ is None
    assert SECRET not in caplog.text


SAFE_MESSAGES = {"bad_model_json": "Модель ответила не по схеме",
                 "bad_provider_reply": "Сценарист недоступен: провайдер вернул ошибку"}


def test_the_wire_carries_the_callers_messages_reminder_and_token(tmp_path):
    """The door passes the caller's messages, retry reminder and the token from <home>/.env."""
    fake = _FakeLlama(chat_payload={"choices": [{"message": {"content": "no json"}}]})
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "q"}]
    try:
        home = _home_with(tmp_path, {"active": "f", "providers": {"f": {
            "type": "openai", "base_url": f"http://127.0.0.1:{fake.port}", "model": "m",
            "api_key_env": "KEY", "shares_gpu": False}}})
        (home / ".env").write_text(f"KEY={SECRET}\n")
        with pytest.raises(StudioError):
            ProviderLlm(home, tmp_path / "gpu-mac.lock").json_turn(msgs, SCHEMA, "only JSON, please")
    finally:
        fake.close()
    first, second = fake.requests
    assert first["body"]["messages"] == msgs
    assert second["body"]["messages"] == [{"role": "system", "content": "only JSON, please"},
                                          *msgs]
    assert first["headers"]["Authorization"] == f"Bearer {SECRET}"


def test_the_lock_wait_is_the_configured_timeout_and_the_lock_is_released(tmp_path):
    import threading
    import time
    lock = tmp_path / "gpu-mac.lock"
    fd = os.open(lock, os.O_RDWR | os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX)
    threading.Timer(0.4, lambda: fcntl.flock(fd, fcntl.LOCK_UN)).start()
    try:
        t0 = time.monotonic()
        with gpu_lock(lock, enabled=True, timeout_s=5.0):
            waited = time.monotonic() - t0
        assert 0.3 < waited < 2.0       # waited for the release, did not give up at once
        with gpu_lock(lock, enabled=True, timeout_s=0.3):
            pass                        # and the first holder let go on exit
    finally:
        os.close(fd)


def test_provider_llm_waits_up_to_its_lock_timeout_for_the_render(tmp_path):
    import threading
    fake = _FakeLlama(chat_payload=_reply({"x": "ok"}))
    lock = tmp_path / "gpu-mac.lock"
    fd = os.open(lock, os.O_RDWR | os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX)
    threading.Timer(0.4, lambda: fcntl.flock(fd, fcntl.LOCK_UN)).start()
    try:
        home = _home_with(tmp_path, {"active": "f", "providers": {"f": {
            "type": "openai", "base_url": f"http://127.0.0.1:{fake.port}", "model": "m",
            "shares_gpu": True}}})
        got = ProviderLlm(home, lock, lock_timeout_s=5.0).json_turn([], SCHEMA, "r")
        assert (got, len(fake.requests)) == ({"x": "ok"}, 1)
    finally:
        os.close(fd)
        fake.close()
