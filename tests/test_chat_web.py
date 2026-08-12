"""Маршруты чата: сессии, ходы, провайдеры, правило «очередь главнее».

**Ни один тест здесь не поднимает настоящий llama-server и не начинает генерацию.** Модель —
`tests/_fake_llama.py` (тот же мок, что в `tests/test_provider.py`), очередь — пустая директория
под `tmp_path`, а «идёт прогон» подменяется на `web._generation_running`: живой прогон стоит
двадцать минут GPU, а проверяемое правило — одна ветка в маршруте.

Три формы повторяются, каждая по причине из `tests/test_web.py`:

* **отказ проверяется по коду, а не по «не 200»** — `409` без `gpu_busy` получается и от сервера,
  который просто не нашёл маршрут;
* **каждое правило проверяется с двух сторон** — «во время прогона локальную модель не поднимаем»
  выполняет и сервер, который вообще ничего не отвечает, поэтому рядом стоит тест о том, что
  внешнему провайдеру тот же прогон не мешает;
* **путь из URL и путь из тела проверяются как пути** — id сессии и путь кадра приходят снаружи и
  превращаются в имя файла, поэтому у обоих есть тест на побег из корня.
"""
import http.client
import json
import threading
from dataclasses import dataclass
from pathlib import Path

import pytest

from _fake_llama import _TURN, _FakeLlama
from h3_48gb import provider
from h3_48gb import queue as q
from h3_48gb import web

#: llama-local без порта: его подставляет `_serve(providers_port=…)`. `/usr/bin/true` вместо
#: настоящего бинаря — если маршрут всё-таки решит запустить процесс, запустится пустышка, а не
#: тридцать гигабайт весов.
_LLAMA = {"type": "llama-local", "llama_server": "/usr/bin/true",
          "presets_ini": "/tmp/presets.ini", "preset": "qwen", "ctx": 4096, "resident_gb": 31}


@dataclass
class _Live:
    """Запущенный сервер и корни, на которые он смотрит, плюс три JSON-хелпера."""

    httpd: object
    port: int
    root: Path          # он же outdir: providers.json, .env и chat/ лежат здесь
    queue_root: Path

    def _request(self, method: str, url: str, payload=None) -> tuple[int, dict]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"} if body is not None else {}
        connection = http.client.HTTPConnection(web.LOOPBACK, self.port, timeout=30)
        try:
            connection.request(method, url, body=body, headers=headers)
            response = connection.getresponse()
            content_type = response.getheader("Content-Type")
            raw = response.read()
            status = response.status
        finally:
            connection.close()
        assert content_type == "application/json", (
            f"{method} {url} ответил {content_type!r}; контракт — JSON везде")
        return status, json.loads(raw)

    def get_json(self, url: str) -> dict:
        status, payload = self._request("GET", url)
        assert status == 200, payload
        return payload

    def get_json_raw(self, url: str) -> tuple[int, dict]:
        return self._request("GET", url)

    def post_json(self, url: str, payload: dict) -> dict:
        status, answer = self._request("POST", url, payload)
        assert status == 200, answer
        return answer

    def post_json_raw(self, url: str, payload: dict) -> tuple[int, dict]:
        return self._request("POST", url, payload)


@pytest.fixture
def _serve(tmp_path):
    """Фабрика серверов на временном outdir.

    `_serve()` — один активный llama-local на порту 0 (всегда «down», никто не слушает);
    `_serve(providers_port=N)` — тот же провайдер на порту мока; `_serve(providers=…, active=…)` —
    любая другая роспись; `_serve(roster=False)` — `providers.json` вообще нет.

    Локальный провайдер по умолчанию, а не пустая роспись: почти каждый тест здесь про ход или
    про отказ хода, а с пустой росписью любой из них упирался бы в `provider_unavailable` раньше
    проверяемого правила — и, например, `gpu_busy` был бы «зелёным» никогда не выполняясь.

    Фабрика, а не готовый сервер: `fake_llama` обязан получить порт раньше, чем сервер — свою
    роспись, а `providers.json` пишется до старта.
    """
    started: list[_Live] = []

    def start(providers_port=None, providers=None, active=None, env=None,
              roster=True) -> _Live:
        root = tmp_path / "outdir"
        root.mkdir(exist_ok=True)
        queue_root = q.layout(root / "queue")["root"]
        if providers is None and roster:
            providers = {"qwen-local": {**_LLAMA, "port": providers_port or 0}}
            active = active or "qwen-local"
        if providers is not None:
            (root / "providers.json").write_text(
                json.dumps({"active": active, "providers": providers}), encoding="utf-8")
        if env is not None:
            (root / ".env").write_text(env, encoding="utf-8")
        httpd = web.make_server(queue_root, root, port=0)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        live = _Live(httpd=httpd, port=httpd.server_address[1], root=root, queue_root=queue_root)
        started.append(live)
        return live

    yield start
    for live in started:
        live.httpd.shutdown()
        live.httpd.server_close()


@pytest.fixture
def fake_llama():
    """Мок llama-server, отвечающий одним и тем же корректным ходом."""
    fake = _FakeLlama(chat_payload=_TURN)
    yield fake
    fake.close()


def _external(port: int) -> dict:
    """Роспись с одним внешним провайдером на `port` — тот же протокол, но без процесса."""
    return {"openrouter": {"type": "openai", "base_url": f"http://127.0.0.1:{port}",
                           "model": "m"}}


# -- сессии ------------------------------------------------------------------------------------


def test_chat_session_is_created_and_survives_a_reload(_serve):
    srv = _serve()
    created = srv.post_json("/api/chat", {"source": {"kind": "new"}, "prompt": ""})
    sid = created["id"]
    got = srv.get_json(f"/api/chat/{sid}")
    assert got["source"] == {"kind": "new"}
    assert got["messages"] == []
    assert (Path(srv.root) / "chat" / f"{sid}.json").is_file()


def test_a_session_remembers_the_mode_and_the_keyframe_it_was_opened_with(_serve):
    """Режим и кадр приходят при создании и живут в сессии: ход к модели собирается из них, а не
    из тела хода, поэтому потеря любого из двух видна только здесь."""
    srv = _serve()
    png = Path(srv.root) / "start.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n0000")
    sid = srv.post_json("/api/chat", {"source": {"kind": "clip", "id": "19-real-run"},
                                      "prompt": "", "mode": "i2v", "image": str(png)})["id"]
    got = srv.get_json(f"/api/chat/{sid}")
    assert got["source"] == {"kind": "clip", "id": "19-real-run"}
    assert got["mode"] == "i2v"
    assert Path(got["image"]) == png.resolve()


def test_a_source_the_server_does_not_know_is_refused_rather_than_stored(_serve):
    """`kind` — закрытый список: сессия с чужим видом источника молча пережила бы страницу,
    которая её открыть уже не сможет."""
    srv = _serve()
    status, payload = srv.post_json_raw("/api/chat", {"source": {"kind": "whatever"},
                                                      "prompt": ""})
    assert status == 400
    assert payload["error"]["code"] == "args_invalid"
    status, payload = srv.post_json_raw("/api/chat", {"source": {"kind": "job", "sql": "drop"},
                                                      "prompt": ""})
    assert (status, payload["error"]["code"]) == (400, "args_invalid")


@pytest.mark.parametrize("sid", ["../../etc/passwd", "..%2f..%2fetc%2fpasswd", "sub/dir"])
def test_a_session_id_cannot_climb_out_of_the_chat_directory(_serve, sid):
    """Id сессии — это имя файла: `../../` в нём обязан быть отказом с кодом, а не 404, который
    неотличим от сервера вообще без проверки путей."""
    srv = _serve()
    status, payload = srv.get_json_raw(f"/api/chat/{sid}")
    assert (status, payload["error"]["code"]) == (400, "path_outside_root")
    status, payload = srv.post_json_raw(f"/api/chat/{sid}/message", {"text": "x", "prompt": ""})
    assert (status, payload["error"]["code"]) == (400, "path_outside_root")


def test_a_message_to_a_session_that_does_not_exist_is_a_named_404(_serve):
    srv = _serve()
    status, payload = srv.get_json_raw("/api/chat/deadbeef")
    assert (status, payload["error"]["code"]) == (404, "chat_not_found")
    status, payload = srv.post_json_raw("/api/chat/deadbeef/message", {"text": "x", "prompt": ""})
    assert (status, payload["error"]["code"]) == (404, "chat_not_found")


# -- очередь главнее ---------------------------------------------------------------------------


def test_a_message_during_a_live_generation_is_refused_and_not_saved(_serve, monkeypatch):
    srv = _serve()
    sid = srv.post_json("/api/chat", {"source": {"kind": "new"}, "prompt": ""})["id"]
    # живой прогон: reconcile().alive непусто — подменяем на уровне web-модуля
    monkeypatch.setattr("h3_48gb.web._generation_running", lambda root: {"job": "j-1"})
    status, payload = srv.post_json_raw(f"/api/chat/{sid}/message",
                                        {"text": "мрачнее", "prompt": "x"})
    assert status == 409
    assert payload["error"]["code"] == "gpu_busy"
    assert srv.get_json(f"/api/chat/{sid}")["messages"] == []


def test_a_live_generation_does_not_stand_in_the_way_of_an_external_provider(_serve, fake_llama,
                                                                             monkeypatch):
    """Обратная сторона правила: 30 ГБ памяти занимает только локальная модель, поэтому чат через
    openrouter во время прогона обязан работать. Без этого теста «gpu_busy всегда» выглядит как
    правильно реализованное правило."""
    srv = _serve(providers=_external(fake_llama.port), active="openrouter")
    sid = srv.post_json("/api/chat", {"source": {"kind": "new"}, "prompt": ""})["id"]
    monkeypatch.setattr("h3_48gb.web._generation_running", lambda root: {"job": "j-1"})
    answer = srv.post_json(f"/api/chat/{sid}/message", {"text": "мрачнее", "prompt": "x"})
    assert answer["reply"] == "Сделал мрачнее."


# -- ход ---------------------------------------------------------------------------------------


def test_a_turn_reaches_the_model_with_the_editors_text_not_the_saved_one(_serve, fake_llama):
    """Правка руками: в окне текст A', в сессии сохранён A — модель обязана видеть A'."""
    srv = _serve(providers_port=fake_llama.port)
    sid = srv.post_json("/api/chat", {"source": {"kind": "new"}, "prompt": "старый"})["id"]
    srv.post_json(f"/api/chat/{sid}/message", {"text": "проверь", "prompt": "правленый руками"})
    sent = fake_llama.requests[-1]["body"]["messages"][0]["content"]
    assert "правленый руками" in sent and "старый" not in sent


def test_the_system_message_carries_the_format_doc_and_the_mode(_serve, fake_llama):
    """Модель без документа формата пишет свободный текст, а без режима — инструкцию для i2v в
    чистом t2va-промпте. Оба куска едут в system, и оба проверяются здесь."""
    srv = _serve(providers_port=fake_llama.port)
    sid = srv.post_json("/api/chat", {"source": {"kind": "new"}, "prompt": "", "mode": "i2v"})["id"]
    srv.post_json(f"/api/chat/{sid}/message", {"text": "опиши", "prompt": "п"})
    system = fake_llama.requests[-1]["body"]["messages"][0]["content"]
    assert "integrated_multimodal_description" in system   # это системный промпт Задачи 4
    assert "mode: i2v" in system
    # режим по умолчанию — t2va, а не пустая строка
    other = srv.post_json("/api/chat", {"source": {"kind": "new"}, "prompt": ""})["id"]
    srv.post_json(f"/api/chat/{other}/message", {"text": "опиши", "prompt": "п"})
    assert "mode: t2va" in fake_llama.requests[-1]["body"]["messages"][0]["content"]


def test_the_history_of_the_session_travels_with_the_next_turn(_serve, fake_llama):
    """Второй ход без первого — это модель с амнезией: «а теперь наоборот» ей не к чему отнести."""
    srv = _serve(providers_port=fake_llama.port)
    sid = srv.post_json("/api/chat", {"source": {"kind": "new"}, "prompt": ""})["id"]
    srv.post_json(f"/api/chat/{sid}/message", {"text": "мрачнее", "prompt": "п"})
    srv.post_json(f"/api/chat/{sid}/message", {"text": "а теперь наоборот", "prompt": "п"})
    roles = [m["role"] for m in fake_llama.requests[-1]["body"]["messages"]]
    assert roles == ["system", "user", "assistant", "user"]
    contents = [m["content"] for m in fake_llama.requests[-1]["body"]["messages"]]
    assert contents[1:] == ["мрачнее", "Сделал мрачнее.", "а теперь наоборот"]


def test_the_reply_and_the_prompt_are_saved_and_survive_a_reload(_serve, fake_llama):
    srv = _serve(providers_port=fake_llama.port)
    sid = srv.post_json("/api/chat", {"source": {"kind": "new"}, "prompt": ""})["id"]
    answer = srv.post_json(f"/api/chat/{sid}/message", {"text": "мрачнее", "prompt": "п"})
    assert answer["reply"] == "Сделал мрачнее."
    assert answer["prompt"]["overall_soundscape"] == "Wind."
    assert answer["llm"]["status"] == "up"
    saved = srv.get_json(f"/api/chat/{sid}")
    assert [(m["role"], m["content"]) for m in saved["messages"]] == [
        ("user", "мрачнее"), ("assistant", "Сделал мрачнее.")]
    assert saved["prompt_struct"]["non_diegetic_music"] == "N/A"


# -- кадр --------------------------------------------------------------------------------------


def test_a_session_with_a_keyframe_attaches_it_to_the_turn_as_an_image_part(_serve, fake_llama):
    srv = _serve(providers_port=fake_llama.port)
    png = Path(srv.root) / "start.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n0000")     # содержимое неважно, важна передача
    sid = srv.post_json("/api/chat", {"source": {"kind": "new"}, "prompt": "",
                                      "mode": "i2v", "image": str(png)})["id"]
    srv.post_json(f"/api/chat/{sid}/message", {"text": "опиши кадр", "prompt": ""})
    content = fake_llama.requests[-1]["body"]["messages"][-1]["content"]
    kinds = [part["type"] for part in content]
    assert kinds == ["text", "image_url"]
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    # история сессии хранит только текст
    saved = srv.get_json(f"/api/chat/{sid}")["messages"][-2]
    assert isinstance(saved["content"], str)


def test_a_keyframe_that_stopped_being_readable_is_a_warning_not_a_refusal(_serve, fake_llama):
    """Кадр удалили между открытием сессии и ходом. Ход всё равно должен дойти — просто без
    картинки и с пометкой, которую страница допишет в ленту."""
    srv = _serve(providers_port=fake_llama.port)
    png = Path(srv.root) / "gone.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n0000")
    sid = srv.post_json("/api/chat", {"source": {"kind": "new"}, "prompt": "",
                                      "mode": "i2v", "image": str(png)})["id"]
    png.unlink()
    answer = srv.post_json(f"/api/chat/{sid}/message", {"text": "опиши кадр", "prompt": ""})
    assert answer["reply"] == "Сделал мрачнее."
    assert "gone.png" in answer["warning"]
    assert isinstance(fake_llama.requests[-1]["body"]["messages"][-1]["content"], str)


def test_a_keyframe_outside_every_root_is_refused_when_the_session_opens(_serve, tmp_path):
    """Путь кадра приходит из тела запроса и читается с диска, а байты уезжают внешнему
    провайдеру: без этой проверки `~/.ssh/id_rsa` уходит в openrouter одним POST'ом."""
    srv = _serve()
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"\x89PNG\r\n\x1a\n0000")
    status, payload = srv.post_json_raw("/api/chat", {"source": {"kind": "new"}, "prompt": "",
                                                      "mode": "i2v", "image": str(outside)})
    assert (status, payload["error"]["code"]) == (400, "path_outside_root")


# -- провайдеры и плашка модели ------------------------------------------------------------------


def test_the_providers_route_lists_the_roster_without_the_token(_serve):
    srv = _serve(providers={**_external(1), "qwen-local": {**_LLAMA, "port": 1}},
                 active="qwen-local", env="OPENROUTER_API_KEY=sk-very-secret\n")
    listed = srv.get_json("/api/providers")
    assert listed["active"] == "qwen-local"
    by_name = {row["name"]: row for row in listed["providers"]}
    assert by_name["qwen-local"]["available"] is True
    assert by_name["openrouter"]["type"] == "openai"
    assert "sk-very-secret" not in json.dumps(listed, ensure_ascii=False)


def test_a_provider_without_its_token_is_refused_with_its_own_reason(_serve):
    srv = _serve(providers={"openrouter": {"type": "openai", "base_url": "http://127.0.0.1:1",
                                           "model": "m", "api_key_env": "OPENROUTER_API_KEY"}},
                 active="openrouter")
    sid = srv.post_json("/api/chat", {"source": {"kind": "new"}, "prompt": ""})["id"]
    status, payload = srv.post_json_raw(f"/api/chat/{sid}/message", {"text": "x", "prompt": ""})
    assert (status, payload["error"]["code"]) == (409, "provider_unavailable")
    assert "OPENROUTER_API_KEY" in payload["error"]["message"]


def test_a_provider_that_does_not_answer_keeps_its_own_code_at_the_http_boundary(_serve,
                                                                                 fake_llama):
    """`ProviderError` — доменное исключение; на границе HTTP оно обязано стать 502 со своим
    кодом, а не `internal_error` от последнего перехватчика."""
    port = fake_llama.port
    fake_llama.close()  # порт больше никто не слушает
    srv = _serve(providers=_external(port), active="openrouter")
    sid = srv.post_json("/api/chat", {"source": {"kind": "new"}, "prompt": ""})["id"]
    status, payload = srv.post_json_raw(f"/api/chat/{sid}/message", {"text": "x", "prompt": ""})
    assert (status, payload["error"]["code"]) == (502, "chat_unreachable")
    assert srv.get_json(f"/api/chat/{sid}")["messages"] == [], "неудачный ход не пишется в историю"


def test_a_model_answering_valid_json_that_is_not_a_turn_is_a_502_not_a_500(_serve):
    """`null` — корректный JSON, и `provider.chat` его разбирает молча. Маршрут обязан назвать
    это ошибкой модели (502 `bad_model_json`), а не своей (`internal_error`, 500)."""
    fake = _FakeLlama(chat_payload={"choices": [{"message": {"content": "null"}}]})
    try:
        srv = _serve(providers=_external(fake.port), active="openrouter")
        sid = srv.post_json("/api/chat", {"source": {"kind": "new"}, "prompt": ""})["id"]
        status, payload = srv.post_json_raw(f"/api/chat/{sid}/message",
                                            {"text": "x", "prompt": ""})
    finally:
        fake.close()
    assert (status, payload["error"]["code"]) == (502, "bad_model_json")


def test_the_llm_plate_follows_the_health_endpoint_and_unload_asks_for_a_shutdown(_serve,
                                                                                  fake_llama,
                                                                                  monkeypatch):
    """`shutdown()` подменён: настоящий делает `pkill -f llama-server` и убил бы модель, поднятую
    на этой машине руками, ради проверки одной строки маршрута."""
    srv = _serve(providers_port=fake_llama.port)
    assert srv.get_json("/api/llm") == {"ok": True, "status": "up", "provider": "qwen-local"}
    killed: list[str] = []
    monkeypatch.setattr(provider.LlamaLocal, "shutdown", lambda self: killed.append(self.name))
    assert srv.post_json("/api/llm/unload", {})["status"] == "down"
    assert killed == ["qwen-local"]


def test_without_a_roster_the_plate_is_down_and_a_turn_says_which_provider_is_missing(_serve):
    """Ни providers.json, ни активного провайдера: сервер обязан отвечать, а не падать."""
    srv = _serve(roster=False)
    assert srv.get_json("/api/llm")["status"] == "down"
    assert srv.get_json("/api/providers") == {"ok": True, "active": None, "providers": []}
    assert srv.post_json("/api/llm/unload", {})["status"] == "down"
    sid = srv.post_json("/api/chat", {"source": {"kind": "new"}, "prompt": ""})["id"]
    status, payload = srv.post_json_raw(f"/api/chat/{sid}/message", {"text": "x", "prompt": ""})
    assert (status, payload["error"]["code"]) == (409, "provider_unavailable")


def test_every_chat_route_names_the_body_fields_it_takes(_serve):
    """Аллоулист полей тела — правило этого сервера (`_json_request`): поле, которое маршрут не
    знает, отказ, а не молчаливое игнорирование."""
    srv = _serve()
    status, payload = srv.post_json_raw("/api/chat", {"source": {"kind": "new"}, "prompt": "",
                                                      "promt": "опечатка"})
    assert (status, payload["error"]["code"]) == (400, "args_invalid")
    sid = srv.post_json("/api/chat", {"source": {"kind": "new"}, "prompt": ""})["id"]
    status, payload = srv.post_json_raw(f"/api/chat/{sid}/message",
                                        {"text": "x", "prompt": "", "sytem": "нет"})
    assert (status, payload["error"]["code"]) == (400, "args_invalid")
